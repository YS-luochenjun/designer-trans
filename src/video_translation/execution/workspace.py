"""Task-local immutable evidence, resource preflight and scoped measurements."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

from PIL import Image

from video_translation.domain.execution import ExecutionProfile, identity
from .media import MediaError


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
    root = Path(root).resolve()
    groups, files, skipped, truncated = {}, 0, 0, False
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in names:
            if files >= max_files:
                truncated = True
                break
            path = Path(directory) / name
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                skipped += 1
                continue
            relative = path.relative_to(root)
            category = ("patch_manifest" if name.endswith(".vpatch.json")
                        else "patch_pixels" if any(p.endswith(".frames") for p in relative.parts)
                        else "evidence_cache" if relative.parts[0] == "frames"
                        else "measurements" if relative.parts[0] == "metrics"
                        else "video" if path.suffix.lower() in {".mp4", ".mkv", ".mov"}
                        else "other_images" if path.suffix.lower() == ".png" else "other")
            try:
                size = path.stat().st_size
            except OSError:
                skipped += 1
                continue
            group = groups.setdefault(category, {"files": 0, "bytes": 0})
            group["files"] += 1
            group["bytes"] += size
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
        self._lock = threading.RLock()
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
        needed = estimated_bytes + self.profile.reserve_bytes
        if available < needed:
            raise MediaError(f"Insufficient workspace space: need={needed}, free={available}")
        return {"estimated_output_bytes": estimated_bytes, "reserve_bytes": self.profile.reserve_bytes,
                "available_bytes": available, "reservation": False}

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
