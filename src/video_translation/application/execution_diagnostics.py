"""Read models for scoped measurements and evidence-bound rework proposals."""
from pathlib import Path
import shutil

from video_translation.domain.execution import EXECUTION_VERSION, ExecutionProfile
from video_translation.execution.workspace import measurements, storage_inventory


def diagnostics(service, task_id, *, include_storage=False):
    task = service.store.require("task", task_id)
    root = (service.settings.runtime / "tasks" / task_id).resolve()
    if not root.is_relative_to(service.settings.runtime.resolve()):
        raise ValueError("Task workspace escaped runtime")
    sessions = [s for s in service.store.list("production_session") if s.get("task_id") == task_id]
    return {**measurements(root), "task_id": task_id,
            "profile": ExecutionProfile.model_validate(task.get("execution_snapshot", {})).model_dump(),
            "execution_version": EXECUTION_VERSION,
            "historical_execution_snapshot": task.get("execution_snapshot"),
            "disk_free_bytes": shutil.disk_usage(service.settings.runtime).free,
            **({"storage": storage_inventory(root)} if include_storage else {}),
            "sessions": [{"id": s["id"], "node_id": s["node_id"], "turn": s["turn"],
                          "base_sha256": s["base"]["sha256"], "accepted_count": len(s.get("accepted", [])),
                          "candidate_id": (s.get("candidate") or {}).get("id"),
                          "incidents": list(s.get("execution_incidents", {}).values())} for s in sessions],
            "quality_passed": False}


def rework_plan(service, task_id, session_id, incident_id, expected_base_sha256):
    """Validate scope/freshness without retrying nodes or changing production."""
    task = service.store.require("task", task_id)
    state = service.store.require("production_session", session_id)
    if state.get("task_id") != task_id:
        raise ValueError("Incident does not belong to the requested task")
    incident = state.get("execution_incidents", {}).get(incident_id)
    if not incident or incident["base_sha256"] != expected_base_sha256:
        raise ValueError("Unknown incident or mismatched expected base")
    base = state["base"]
    if base["sha256"] != expected_base_sha256:
        raise ValueError("Rework proposal is stale after a base change")
    current = state.get("execution_binding", {})
    if any(current.get(field, incident.get(field)) != incident.get(field)
           for field in ("scope_sha256", "capability_sha256")):
        raise ValueError("Rework proposal is stale after a scope or capability change")
    path = Path(base["path"])
    allowed = (service.settings.runtime / "tasks" / task_id).resolve()
    assets = [service.checked_path(service.store.require("asset", a)) for a in task["asset_ids"]]
    if path.is_symlink() or (not path.resolve().is_relative_to(allowed) and path.resolve() not in assets):
        raise ValueError("Rework base is outside the task")
    if service.hash_file(path) != base["sha256"]:
        raise ValueError("Rework base file changed")
    if str(path).endswith(".vpatch.json"):
        from video_translation.execution.media import MediaTools
        from video_translation.execution.efficient_media import visual_for
        from types import SimpleNamespace
        visual_for(SimpleNamespace(service=service, store=service.store,
                                   media=MediaTools(service.settings.ffmpeg, service.settings.ffprobe)), task)._info(path)
    return {"revision": 1, "task_id": task_id, "session_id": session_id,
            "incident_id": incident_id, "plan": incident["rework"],
            "source_ids": incident["unresolved_source_ids"], "applied": False,
            "quality_passed": False, "blocked_by_control": task.get("control_paused", False)
                or task["status"] == "cancelled",
            "production_mutations": False}
