"""Task-local immutable evidence, resource preflight and scoped measurements."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

from PIL import Image

from video_translation.domain.execution import ExecutionProfile, identity
from .media import MediaError

_ROOT_LOCKS = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish_json(path, value):
    """Publish a new immutable record, never overwrite an existing artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False).encode()
    with path.open("xb") as stream:
        stream.write(data)


def measurements(root):
    """Read incomplete metrics conservatively; never create a workspace on query."""
    phases, malformed = {}, 0
    root = Path(root).resolve()
    for path in (root / "metrics").glob("*.json"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        try:
            if path.stat().st_size > 64 * 1024:
                raise ValueError("Oversized metric")
            row = json.loads(path.read_bytes())
            phase = phases.setdefault(row["phase"], {"count": 0, "failed": 0,
                "elapsed_seconds": 0, "cache_hits": 0, "bytes_written": 0})
            phase["count"] += 1
            phase["failed"] += row["status"] == "failed"
            for key in ("elapsed_seconds", "cache_hits", "bytes_written"):
                phase[key] += row.get(key, 0)
            for key in ("source_frames", "eligible_frames", "transferred_frames",
                        "packet_bytes", "supplied_images"):
                if key in row:
                    phase[key] = phase.get(key, 0) + row[key]
        except (ValueError, KeyError, TypeError, OSError):
            malformed += 1
    return {"revision": 1, "phases": phases, "malformed_metrics": malformed,
            "timing_scope": "instrumented local intervals only; nested phases are not additive",
            "quality_passed": False}


def workspace_for(service, task):
    profile = ExecutionProfile.model_validate(task.get("execution_snapshot", {}))
    return ExecutionWorkspace(service.settings.runtime / "tasks" / task["id"], profile)


def storage_inventory(root, *, max_files=100_000):
    """Logical task-file bytes, bounded scan; no hash, deletion or retention claim."""
    if Path(root).is_symlink() or Path(root).is_junction():
        return {"groups": {}, "logical_bytes": 0, "files": 0, "skipped": 1, "truncated": False,
                "scope": "linked inventory root rejected"}
    root = Path(root).resolve()
    groups, files, skipped, truncated = {}, 0, 0, False
    def unreadable(_error):
        nonlocal skipped
        skipped += 1

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=unreadable):
        directory = Path(directory)
        if not directory.resolve().is_relative_to(root):
            skipped += 1
            dirs.clear()
            continue
        linked = [d for d in dirs if ((Path(directory) / d).is_symlink()
                                     or (Path(directory) / d).is_junction())]
        skipped += len(linked)
        dirs[:] = [d for d in dirs if d not in linked]
        for name in names:
            if files >= max_files:
                truncated = True
                break
            path = Path(directory) / name
            try:
                info = path.lstat()
            except OSError:
                skipped += 1
                continue
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                skipped += 1
                continue
            relative = path.relative_to(root)
            category = ("patch_manifest" if name.endswith(".vpatch.json")
                        else "patch_pixels" if any(p.endswith(".frames") for p in relative.parts)
                        else "evidence_cache" if relative.parts[0] == "frames"
                        else "measurements" if relative.parts[0] == "metrics"
                        else "video" if path.suffix.lower() in {".mp4", ".mkv", ".mov"}
                        else "other_images" if path.suffix.lower() == ".png" else "other")
            group = groups.setdefault(category, {"files": 0, "bytes": 0})
            group["files"] += 1
            group["bytes"] += info.st_size
            files += 1
        if truncated:
            break
    return {"groups": groups, "logical_bytes": sum(g["bytes"] for g in groups.values()),
            "files": files, "skipped": skipped, "truncated": truncated,
            "scope": "task files; logical lengths, not allocated/peak disk or safe deletion candidates"}


class ExecutionWorkspace:
    def __init__(self, root, profile=None):
        self.root = Path(root).resolve()
        self.profile = profile or ExecutionProfile(mode="efficient_v1")
        self.root.mkdir(parents=True, exist_ok=True)
        with _ROOT_LOCKS_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(str(self.root), threading.RLock())
        self.metrics_dropped = 0

    def confined(self, path):
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("Execution artifact escaped its task workspace")
        return path.resolve()

    def preflight(self, output, estimated_bytes):
        if type(estimated_bytes) is not int or estimated_bytes < 0:
            raise ValueError("Space estimate must be a nonnegative integer")
        output = self.confined(output)
        parent = output.parent
        while not parent.exists():
            parent = parent.parent
        available = shutil.disk_usage(parent).free
        budget = self.profile.resource_budget
        reserve = max(self.profile.reserve_bytes, budget.min_free_bytes if budget else 0)
        needed = estimated_bytes + reserve
        if available < needed:
            raise MediaError(f"Insufficient workspace space: need={needed}, free={available}")
        inventory = None
        if budget:
            inventory = storage_inventory(self.root, max_files=budget.max_workspace_files + 1)
            if self.root.parent.name == "tasks":
                related = self.root.parent.parent / "route" / self.root.name
                if related.exists():
                    extra = storage_inventory(related, max_files=budget.max_workspace_files + 1)
                    inventory = {
                        "groups": {**inventory["groups"],
                                   **{"route/" + key: value for key, value in extra["groups"].items()}},
                        "files": inventory["files"] + extra["files"],
                        "logical_bytes": inventory["logical_bytes"] + extra["logical_bytes"],
                        "skipped": inventory["skipped"] + extra["skipped"],
                        "truncated": inventory["truncated"] or extra["truncated"],
                    }
            if inventory["skipped"] or inventory["truncated"] or inventory["files"] >= budget.max_workspace_files:
                raise MediaError("Task workspace file allowance reached or inventory is incomplete")
            if inventory["logical_bytes"] + estimated_bytes > budget.max_workspace_bytes:
                raise MediaError("Task workspace byte allowance reached; retain current results")
        return {"estimated_output_bytes": estimated_bytes, "reserve_bytes": reserve,
                "workspace_logical_bytes": inventory["logical_bytes"] if inventory else None,
                "available_bytes": available, "reservation": False}

    @contextmanager
    def local_write(self):
        """Serialize managed local media writes; model/provider waits stay independent."""
        with self._lock:
            yield

    @contextmanager
    def measure(self, phase, **fields):
        if phase not in {"capture", "render", "decode", "model_operation", "patch", "materialize",
                         "evaluation", "node", "prepare"}:
            raise ValueError("Unknown measured phase")
        started = time.perf_counter()
        result = {"revision": 1, "phase": phase, "status": "running", **fields}
        try:
            yield result
            result["status"] = "succeeded"
        except Exception as exc:
            result.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            result["elapsed_seconds"] = time.perf_counter() - started
            result["timing_scope"] = "local operation interval; nested intervals are not additive"
            try:
                publish_json(self.root / "metrics" / f"{uuid4().hex}.json", result)
            except OSError:
                # An observational write must not replace a media failure or receipt.
                self.metrics_dropped += 1

    def frame(self, source_sha, frame_index, decoder, produce):
        key = identity({"revision": 1, "source_sha256": source_sha,
                        "frame_index": frame_index, "decoder": decoder})
        directory = self.root / "frames" / key
        image_path, receipt_path = directory / "frame.png", directory / "receipt.json"
        with self._lock:
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text("utf-8"))
                if receipt.get("key") != key or file_sha(image_path) != receipt.get("sha256"):
                    raise MediaError("Cached evidence failed identity verification")
                return image_path, receipt["sha256"], True
            self.preflight(image_path, 32 * 1024**2)
            directory.mkdir(parents=True, exist_ok=True)
            lock = directory / "publish.lock"
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise MediaError("Evidence publication is pending; reconcile original publisher") from None
            try:
                os.close(descriptor)
                with TemporaryDirectory(prefix="frame-", dir=directory) as scratch:
                    temporary = Path(scratch) / "frame.png"
                    produce(temporary)
                    with Image.open(temporary) as image:
                        image.verify()
                    digest = file_sha(temporary)
                    # A missing receipt after interruption may leave an identical orphan.
                    if image_path.exists():
                        if file_sha(image_path) != digest:
                            raise MediaError("Unsettled frame artifact differs from resumed output")
                    else:
                        os.replace(temporary, image_path)
                    publish_json(receipt_path, {"revision": 1, "key": key, "sha256": digest,
                                                "frame_index": frame_index, "source_sha256": source_sha})
            finally:
                lock.unlink()
            return image_path, digest, False

    def summary(self):
        return {**measurements(self.root), "metrics_dropped_current_instance": self.metrics_dropped}
