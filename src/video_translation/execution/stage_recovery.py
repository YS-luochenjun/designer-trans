"""Recheck retained node outputs at startup without accepting or deleting evidence."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from video_translation.integrations.agents import CapabilityUnavailable
from .stage_quality import require_node_output


def reconcile_stage_outputs(service, task):
    if (task.get("visual_route") != "agent_adaptive" or task.get("control_paused")
            or task["status"] in {"cancelled", "completed"}
            or any(n["status"] in {"running", "waiting_external"} for n in task["nodes"])):
        return
    sessions = {s["node_id"]: s for s in service.store.list("production_session")
                if s["task_id"] == task["id"]}
    for node in task["nodes"]:
        reconsider_failed_clean = (node["definition_id"] == "clean"
                                    and node["status"] == "failed"
                                    and node.get("stage_readiness") == "needs_repair")
        if ((node["status"] != "succeeded" and not reconsider_failed_clean)
                or node["definition_id"] not in {"clean", "typography", "visual"}):
            continue
        try:
            require_node_output(task, node, node["output"])
            continue
        except CapabilityUnavailable as exc:
            reason = str(exc)
        reopen = False
        if node["definition_id"] == "clean":
            from .review_draft import handoff_needs_capability_review
            state = sessions.get(node["id"]) or {}
            base = state.get("base") or {}
            controller = SimpleNamespace(task=task, node=node, store=service.store)
            current_decision = any(
                d.get("task_id") == task["id"] and d.get("node_id") == node["id"]
                and d.get("key") == f"clean-{state.get('turn')}"
                and d.get("status") in {"running", "unknown", "succeeded", "invalid_response"}
                for d in service.store.list("production_decision"))
            path = Path(base.get("path", ""))
            root = service.settings.runtime / "tasks" / task["id"]
            reopen = bool(
                state and not state.get("candidate") and state.get("pending_turn") is None
                and not current_decision and base.get("sha256") == node["output"].get("sha256")
                and path.resolve().is_relative_to(root.resolve()) and path.is_file()
                and service.hash_file(path) == base["sha256"]
                and handoff_needs_capability_review(controller, state))
        elif node["definition_id"] == "typography" and node["output"].get("render_error"):
            reopen = all(next(n for n in task["nodes"] if n["id"] == dep)["status"]
                         in {"succeeded", "skipped"} for dep in node["depends_on"])
        if reconsider_failed_clean and not reopen:
            continue
        _retain(node)
        node.update(status="pending" if reopen else "failed", stage_readiness="needs_repair",
                    error=reason, finished_at=None)
        # Only descendants lose readiness. Audio, translation and source analysis
        # remain reusable; unknown calls keep their original status and identity.
        descendants = {node["id"]}
        for child in task["nodes"]:
            if child["id"] == node["id"] or not descendants.intersection(child["depends_on"]):
                continue
            descendants.add(child["id"])
            child["stale_upstream"] = node["id"]
            if child["status"] in {"succeeded", "skipped", "failed", "interrupted"}:
                _retain(child)
                child.update(status="pending", stage_readiness="awaiting_upstream",
                             error=None, finished_at=None)


def _retain(node):
    node.setdefault("history", []).append({
        **{k: deepcopy(node.get(k)) for k in ("attempt", "status", "output", "artifact_ids", "error")},
        "reason": "Node output did not satisfy its production stage contract",
    })
