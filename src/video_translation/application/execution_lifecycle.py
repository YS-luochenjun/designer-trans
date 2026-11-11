"""Explicit local previews and conservative removal of unreferenced frame caches."""
import json
from pathlib import Path
from types import SimpleNamespace

from video_translation.domain.execution import identity
from video_translation.execution.efficient_media import visual_for
from video_translation.execution.media import MediaTools
from video_translation.execution.workspace import file_sha
from video_translation.persistence.store import now
from .tasks import Conflict


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def preview_representation(service, task_id, session_id, expected_base_sha256):
    task = service.store.require("task", task_id)
    session = service.store.require("production_session", session_id)
    if session["task_id"] != task_id or session["base"]["sha256"] != expected_base_sha256:
        raise Conflict("Preview requires the current bound representation")
    if task["status"] == "cancelled":
        raise Conflict("Task cancelled")
    visual = visual_for(SimpleNamespace(service=service, store=service.store,
        media=MediaTools(service.settings.ffmpeg, service.settings.ffprobe)), task)
    source = visual._allowed(session["base"]["path"])
    if file_sha(source) != expected_base_sha256:
        raise Conflict("Preview base changed")
    output, receipt = visual.checkpoint(source)
    key = identity([task_id, session_id, expected_base_sha256])
    previous = service.store.get("execution_preview", key)
    if previous:
        return previous
    node = next(n for n in task["nodes"] if n["id"] == session["node_id"])
    artifact = service.artifact(task_id, node, output, "video", "Representation preview")
    return service.store.put("execution_preview", {"id": key, "task_id": task_id, "created_at": now(),
        "session_id": session_id, "base_sha256": expected_base_sha256, "artifact_id": artifact["id"],
        "content_url": artifact["content_url"], "representation_receipt": receipt,
        "quality_passed": False, "human_approved": False, "scheduled_new_calls": False})


def cache_retention_plan(service, task_id):
    task = service.store.require("task", task_id)
    if task["status"] not in {"completed", "cancelled"} or any(
            n["status"] in {"running", "unknown", "waiting_external"} for n in task["nodes"]):
        raise Conflict("Retention requires a terminal task with no in-flight or unknown work")
    for kind in ("operation", "production_decision", "imagegen_request"):
        if any(row.get("task_id") == task_id and row.get("status") in {
                "running", "unknown", "invalid_response", "pending", "claimed"} for row in service.store.list(kind)):
            raise Conflict("Unsettled receipts prevent cache retirement")
    from .deletion import TaskDeletionService
    path_guard = TaskDeletionService(service.settings, service.store)
    root = path_guard._check_path(service.settings.runtime / "tasks" / task_id).resolve()
    protected = set()
    # Include references from other branches, not just records owned by this task.
    with service.store.connection() as db:
        for (data,) in db.execute("SELECT data FROM records"):
            for text in strings(json.loads(data)):
                try:
                    path = Path(text)
                    if path.is_absolute() and path.resolve().is_relative_to(root):
                        protected.add(str(path.resolve()))
                except (OSError, ValueError):
                    continue
    # A manifest may retain pixels without those paths appearing in DB records.
    for manifest in root.rglob("*.vpatch.json"):
        if manifest.is_symlink() or manifest.stat().st_size > 2 * 1024**2:
            raise Conflict("Unsafe or oversized representation prevents retention")
        for text in strings(json.loads(manifest.read_bytes())):
            try:
                path = Path(text)
                if path.is_absolute() and path.resolve().is_relative_to(root):
                    protected.add(str(path.resolve()))
            except (OSError, ValueError):
                continue
    # Original work packets may be the only durable image-path reference.
    pending, visited = list(protected), set()
    while pending:
        text = pending.pop()
        if text in visited or not text.endswith(".json"):
            continue
        visited.add(text)
        if len(visited) > 100_000:
            raise Conflict("Retention reference scan exceeded its safety bound")
        path = path_guard._check_path(text)
        if path.stat().st_size > 2 * 1024**2:
            raise Conflict("Referenced JSON is too large for safe retention analysis")
        try:
            values = json.loads(path.read_bytes())
        except ValueError:
            raise Conflict("Referenced evidence JSON cannot be parsed; retain its dependencies") from None
        for value in strings(values):
            try:
                child = Path(value)
                if child.is_absolute() and child.resolve().is_relative_to(root):
                    resolved = str(child.resolve())
                    if resolved not in protected:
                        protected.add(resolved)
                        pending.append(resolved)
            except (OSError, ValueError):
                continue
    candidates = []
    for receipt in sorted((root / "frames").glob("*/receipt.json")):
        directory = receipt.parent
        path_guard._check_path(directory)
        if any(p.is_symlink() for p in (root, root / "frames", directory, receipt)):
            raise Conflict("Linked cache cannot be retired")
        files = sorted(directory.iterdir())
        for path in files:
            path_guard._check_path(path)
        if {p.name for p in files} != {"frame.png", "receipt.json"}:
            continue
        image = directory / "frame.png"
        if str(image.resolve()) in protected or str(receipt.resolve()) in protected:
            continue
        row = json.loads(receipt.read_bytes())
        if file_sha(image) != row["sha256"]:
            raise Conflict("Orphan cache changed; reconcile its original publisher")
        candidates.extend({"path": str(p), "sha256": file_sha(p), "bytes": p.stat().st_size} for p in files)
    result = {"task_id": task_id, "task_basis": identity(task), "files": candidates,
              "protected_sha256": identity(sorted(protected)), "bytes": sum(p["bytes"] for p in candidates),
              "scope": "unreferenced frame caches only; no media, traces, masks, receipts or metrics",
              "automatic": False}
    return {**result, "plan_sha256": identity(result)}


def prune_cache(service, task_id, expected_plan, *, approve=False):
    if not approve:
        raise Conflict("Explicit confirmation of the fresh retention plan is required")
    plan = cache_retention_plan(service, task_id)
    if plan["plan_sha256"] != expected_plan:
        raise Conflict("Retention plan changed; no files removed")
    key = identity([task_id, expected_plan])
    if service.store.get("cache_retention", key):
        raise Conflict("Original retirement exists; reconcile it rather than replaying deletion")
    service.store.put("cache_retention", {"id": key, "task_id": task_id, "created_at": now(),
        "status": "running", "plan": plan})
    try:
        for row in plan["files"]:
            path = Path(row["path"])
            from .deletion import TaskDeletionService
            TaskDeletionService(service.settings, service.store)._check_path(path)
            # No recursive deletion; only the exact hash-checked cache files.
            if path.is_symlink() or file_sha(path) != row["sha256"]:
                raise Conflict("Cache changed during retirement")
            path.unlink()
        service.store.update("cache_retention", key, lambda r: r.update(status="succeeded", finished_at=now()))
    except BaseException:
        service.store.update("cache_retention", key, lambda r: r.update(status="unknown"))
        raise
    return {"id": key, "bytes_removed": plan["bytes"], "quality_passed": False}
