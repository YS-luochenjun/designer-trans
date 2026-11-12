"""Explicit retirement of historical video pixels; facts and active media survive."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from video_translation.domain.execution import identity
from video_translation.persistence.store import now
from .tasks import Conflict

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
UNSETTLED = {"running", "unknown", "pending", "claimed", "invalid_response", "waiting_external"}
HISTORY_FIELDS = {"history", "accepted", "candidate_archive", "historical_candidates",
                  "repair_outcomes", "draft_preview_archive", "context_projection"}


def _strings(value, *, active=False):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if not active or key not in HISTORY_FIELDS:
                yield from _strings(child, active=active)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child, active=active)


def _checked(path, root, *, file=False):
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root) or path == root:
        raise Conflict("Storage path leaves the exact task directory")
    for item in (path, *path.parents):
        if item == root.parent:
            break
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise Conflict("Linked storage paths cannot be retired")
    if file and (not path.is_file() or path.is_symlink()):
        raise Conflict("Storage file changed or disappeared")
    return path


def _quiet(task, records):
    if not task.get("control_paused") and task["status"] not in {"completed", "cancelled"}:
        raise Conflict("Historical retirement requires a paused or terminal task")
    if any(n["status"] in {"running", "waiting_external"} for n in task["nodes"]):
        raise Conflict("Task still has an active writer")
    if any(row.get("task_id") == task["id"] and row.get("status") in {"running", "pending", "claimed"}
           for kind, _, row in records if kind in {
               "operation", "production_decision", "imagegen_request", "route_run", "route_probe"}):
        raise Conflict("An external or recovery operation still needs to finish")


def _records(store):
    with store.connection() as db:
        return [(kind, key, json.loads(data))
                for kind, key, data in db.execute("SELECT kind,id,data FROM records ORDER BY kind,id")]


def _stamp(path):
    info = path.stat()
    return {"path": str(path), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
            "inode": info.st_ino, "device": info.st_dev}


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def history_plan(service, task_id, *, keep_recent_per_source=2, hash_candidates=True):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
        raise ValueError("Invalid task identity")
    if not 1 <= keep_recent_per_source <= 20:
        raise ValueError("Keep between one and twenty recent candidates per source")
    records = _records(service.store)
    task = next((row for kind, key, row in records if kind == "task" and key == task_id), None)
    if task is None:
        raise KeyError(f"task: {task_id}")
    _quiet(task, records)
    runtime = service.settings.runtime.resolve()
    root = _checked(runtime / "tasks" / task_id, runtime)
    if root.exists() and not root.is_dir():
        raise Conflict("Task storage root must be a directory")
    protected, reasons, active_json = set(), {}, []
    task_rows = [(kind, key, row) for kind, key, row in records
                 if row.get("task_id") == task_id or kind == "task" and key == task_id]
    unresolved_ids = set()

    def protect(value, reason, *, active=False):
        for text in _strings(value, active=active):
            try:
                path = Path(text)
                if not path.is_absolute():
                    continue
                path = Path(os.path.abspath(path))
                if not path.is_relative_to(root):
                    continue
                _checked(path, root)
                if path in protected:
                    continue
                protected.add(path)
                reasons[str(path)] = reason
                dependency_input = path.name.endswith((".vpatch.json", "work-packet.json", "request.json"))
                if (path.suffix == ".json" and path.is_file()
                        and (reason != "original unsettled attempt" or dependency_input)):
                    active_json.append(path)
            except (OSError, ValueError):
                continue

    protect(task, "current task outputs", active=True)
    for kind, _, row in task_rows:
        if kind == "production_session":
            protect({key: row.get(key) for key in (
                "base", "candidate", "scope_review", "route_work_order", "execution_checkpoints")},
                "current plate, candidate or explicit handoff", active=True)
            pins = {row.get("archive_under_review")}
            pins.update(d.get("accepted_id") for d in row.get("acceptance_disputes", [])
                        if d.get("status") != "resolved_by_current_plate_review")
            keep = {}
            for item in reversed(row.get("candidate_archive", [])):
                source = item.get("source_id")
                count = keep.get(source, 0)
                if count < keep_recent_per_source or item.get("id") in pins:
                    protect(item, "recent candidate or unresolved quality dispute")
                    keep[source] = count + 1
            for item in row.get("accepted", []):
                if item.get("id") in pins:
                    protect(item, "unresolved accepted-result dispute")
                    for candidate in row.get("candidate_archive", []):
                        if candidate.get("sha256") == item.get("sha256"):
                            protect(candidate, "unresolved accepted-result pixels")
        elif kind in {"imagegen_request", "clean_reference", "production_image", "production_layer",
                      "local_candidate", "adaptive_audio_prepare", "adaptive_audio_segment", "work_handoff"}:
            protect(row, "retained image, audio, layer or explicit local candidate")
        if row.get("status") in UNSETTLED:
            unresolved_ids.add(row["id"])
            protect(row, "unsettled original operation", active=True)
    # Other tasks and branches retain their exact references, including their history.
    for kind, key, row in records:
        owner = row.get("task_id") or (key if kind == "task" else None)
        if owner and owner != task_id:
            protect(row, "cross-task dependency")

    all_files, errors = [], []
    def scan_error(error):
        errors.append(type(error).__name__)
    for directory, names, files in os.walk(root, followlinks=False, onerror=scan_error) if root.exists() else ():
        for name in names:
            _checked(Path(directory) / name, root)
        for name in files:
            path = _checked(Path(directory) / name, root, file=True)
            all_files.append(path)
    if errors:
        raise Conflict("Incomplete storage enumeration; no retirement plan")
    # An unknown call keeps its original attempt and the active dependencies in its packet.
    prefixes = tuple(key[:20] for key in unresolved_ids)
    original_directories = set()
    if prefixes:
        for path in all_files:
            if path.name.startswith(prefixes):
                relative = path.relative_to(root).parts
                if len(relative) >= 4 and relative[0] == "nodes":
                    original_directories.add(root.joinpath(*relative[:3]))
                else:
                    original_directories.add(path.parent)
    for path in all_files:
        if any(path.is_relative_to(directory) for directory in original_directories):
            protect(str(path), "original unsettled attempt")
    visited = set()
    while active_json:
        path = active_json.pop()
        if path in visited:
            continue
        visited.add(path)
        if len(visited) > 20_000 or path.stat().st_size > 16 * 1024**2:
            raise Conflict("Active dependency metadata exceeds analysis bound")
        try:
            protect(json.loads(path.read_bytes()), "active file dependency", active=True)
        except (ValueError, OSError) as exc:
            raise Conflict(f"Active dependency metadata cannot be verified: {path.relative_to(root)}") from exc
    candidates = []
    protected_videos = []
    for path in sorted(all_files):
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        if path in protected:
            protected_videos.append({"path": str(path), "bytes": path.stat().st_size,
                                     "reason": reasons[str(path)]})
            continue
        # Only historical node videos; previews, final delivery and shared assets survive.
        if path.relative_to(root).parts[0] != "nodes":
            continue
        row = _stamp(path)
        row["sha256"] = _sha(path) if hash_candidates else None
        candidates.append(row)
    related_basis = [(kind, key, identity(row)) for kind, key, row in records
                     if kind not in {"storage_retirement", "storage_plan"}
                     and (row.get("task_id") == task_id or kind == "task" and key == task_id)]
    basis = {
        "revision": 1, "task_id": task_id, "root": str(root),
        "records_sha256": identity(related_basis), "keep_recent_per_source": keep_recent_per_source,
        "files": candidates, "protected_videos": protected_videos,
        "protection_sha256": identity([_stamp(path) for path in sorted(protected) if path.is_file()]),
        "scope": "historical node video pixels only; all metadata, images, audio and active dependencies retained",
        "impact": "Retired historical videos cannot be replayed or visually compared; original facts and costs remain.",
        "hashes_complete": hash_candidates,
    }
    return {**basis, "plan_sha256": identity(basis), "created_at": now(),
            "estimated_reclaim_bytes": sum(row["bytes"] for row in candidates),
            "task_logical_bytes": sum(path.stat().st_size for path in all_files),
            "task_files": len(all_files), "disk_free_bytes": shutil.disk_usage(runtime).free}


def apply_history_plan(service, plan, *, approve=False, guard=None):
    if not approve or not plan.get("hashes_complete"):
        raise Conflict("Explicit approval of a fully hashed historical retirement plan is required")
    expected = plan["plan_sha256"]
    content = {key: value for key, value in plan.items()
               if key not in {"plan_sha256", "created_at", "estimated_reclaim_bytes",
                              "task_logical_bytes", "task_files", "disk_free_bytes"}}
    if identity(content) != expected:
        raise Conflict("Retirement plan content changed")
    # The API supplies the dispatch fence; the offline CLI must own worker.lock.
    if not callable(guard):
        raise Conflict("Retirement requires an exclusive worker fence")
    with guard(plan["task_id"]), service.store.lock:
        key = identity(["historical-video-retirement", plan["task_id"], expected])
        if service.store.get("storage_retirement", key):
            raise Conflict("Original retirement exists; reconcile its receipt instead of replaying")
        current = history_plan(service, plan["task_id"],
                               keep_recent_per_source=plan["keep_recent_per_source"])
        if current["plan_sha256"] != expected:
            raise Conflict("Retirement basis changed; generate a fresh plan")
        before = shutil.disk_usage(plan["root"]).free
        receipt = {"id": key, "task_id": plan["task_id"], "created_at": now(),
                   "status": "running", "plan_sha256": expected, "impact": plan["impact"],
                   "files": plan["files"], "removed": [], "bytes_removed": 0}
        # Persist the complete exact file/hash intent before the first unlink.
        service.store.put("storage_retirement", receipt)
        try:
            for row in plan["files"]:
                task = service.store.require("task", plan["task_id"])
                if not task.get("control_paused") and task["status"] not in {"completed", "cancelled"}:
                    raise Conflict("User resumed the task; retirement stopped")
                path = _checked(row["path"], Path(plan["root"]), file=True)
                stamp = _stamp(path)
                if stamp != {key: row[key] for key in stamp} or _sha(path) != row["sha256"]:
                    raise Conflict("Historical video changed; retirement stopped")
                path.unlink()
                receipt["removed"].append(row["path"])
                receipt["bytes_removed"] += row["bytes"]
                service.store.put("storage_retirement", receipt)
        except BaseException:
            receipt.update(status="partial", finished_at=now())
            service.store.put("storage_retirement", receipt)
            raise
        receipt.update(status="succeeded", finished_at=now(),
                       disk_free_before=before, disk_free_after=shutil.disk_usage(plan["root"]).free)
        service.store.put("storage_retirement", receipt)
        return receipt


def retirement_for(store, path):
    target = str(Path(path).resolve())
    for receipt in store.list("storage_retirement"):
        if target in receipt.get("removed", []):
            return {"receipt_id": receipt["id"], "plan_sha256": receipt["plan_sha256"],
                    "state": "historical_pixels_retired", "impact": receipt["impact"]}
        if (receipt["status"] in {"running", "partial"}
                and any(row["path"] == target for row in receipt["files"])):
            return {"receipt_id": receipt["id"], "plan_sha256": receipt["plan_sha256"],
                    "state": "retirement_requires_reconciliation", "impact": receipt["impact"]}
    return None


def retired_candidates(store, state):
    removed = set()
    receipt_ids = []
    for receipt in store.list("storage_retirement"):
        if receipt["task_id"] == state["task_id"] and receipt.get("removed"):
            removed.update(receipt["removed"])
            receipt_ids.append(receipt["id"])
    return {
        "receipt_ids": receipt_ids,
        "unavailable_candidate_ids": [row["id"] for row in state.get("candidate_archive", [])
                                      if row.get("path") in removed],
        "rule": "Retired historical pixels are unavailable for replay or acceptance. Facts and fees remain; "
                "do not regenerate the same retired candidate merely to restore a file.",
    }
