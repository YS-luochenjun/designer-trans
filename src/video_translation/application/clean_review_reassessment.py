"""Re-review retained pixels under an explicit viewing standard, without generation."""
import json
from copy import deepcopy

from video_translation.agent_runtime.resources import ResourceCatalog
from video_translation.execution.clean_policy import QUALITY_RESOURCE_ID
from video_translation.execution.clean_progress import fingerprint
from video_translation.execution.viewing_review import enabled
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status
from .tasks import Conflict


def reassess_clean_review(service, task_id, candidate_id, expected_base_sha256):
    """Caller holds worker guard; request contains identity only, never a verdict."""
    from video_translation.execution.adaptive import AdaptiveProduction
    from video_translation.execution.nodes import NodeExecutor

    task = service.store.require("task", task_id)
    sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
    if len(sessions) != 1:
        raise Conflict("Review reassessment requires one clean session")
    state = sessions[0]
    before_task, before_state = deepcopy(task), deepcopy(state)
    node = next(n for n in task["nodes"] if n["id"] == state["node_id"])
    if (task["status"] != "blocked" or task.get("control_paused")
            or node["definition_id"] != "clean" or node["status"] != "failed"
            or state.get("candidate") or state.get("pending_turn") is not None
            or any(n["status"] in {"running", "unknown", "waiting_external"}
                   or n.get("external_pending") for n in task["nodes"])):
        raise Conflict("Review requires a stopped, unpaused clean task without pending results")
    if state["base"]["sha256"] != expected_base_sha256:
        raise Conflict("Current plate changed")
    if any(d.get("node_id") == node["id"] and d.get("key") == f"clean-{state['turn']}"
           and d["status"] in {"running", "unknown", "succeeded"}
           for d in service.store.list("production_decision")):
        raise Conflict("Reconcile the current work result first")
    archives = [c for c in state.get("candidate_archive", []) if c["id"] == candidate_id]
    if (len(archives) != 1 or archives[0].get("review_status") != "rejected"
            or archives[0].get("input_sha256") != expected_base_sha256
            or any(a["id"] == candidate_id for a in state["accepted"])):
        raise Conflict("Select one rejected candidate on the unchanged current plate")
    controller = AdaptiveProduction(NodeExecutor(service), task, node)
    controller.verified(state["base"])
    controller.verified(archives[0])
    policy = next(r for r in ResourceCatalog(service.settings.root).list("knowledge")
                  if r["id"] == QUALITY_RESOURCE_ID and r["status"] == "active")
    if not enabled(json.loads(policy["content"])):
        raise Conflict("Viewing-impact review standard is not available")
    key = fingerprint({"candidate": archives[0]["sha256"], "base": expected_base_sha256,
                       "policy": policy["sha256"]})
    if key in state.get("quality_reassessments", {}):
        raise Conflict("This candidate already received this review standard; no repeated reset")
    receipt = {"key": key, "candidate_id": candidate_id,
               "candidate_sha256": archives[0]["sha256"], "base_sha256": expected_base_sha256,
               "policy": {k: policy[k] for k in ("id", "version", "sha256")},
               "at": now(), "generation_authorized": False, "recipe_supplied": False,
               "historical_rejection_retained": True}
    state.setdefault("quality_reassessments", {})[key] = receipt
    state["candidate"] = {**deepcopy(archives[0]), "review_status": "needs_review",
                          "quality_reassessment": receipt}
    state["times_ms"] = controller.candidate_times(state["candidate"])
    task.setdefault("resource_upgrades", []).append({
        "at": now(), "reason": "User requested viewing-impact calibration; re-review retained media",
        "previous_quality_resources": deepcopy([
            r for r in task["resource_snapshot"]["knowledge"] if r["id"] == QUALITY_RESOURCE_ID]),
        "review_key": key})
    task["resource_snapshot"]["knowledge"] = [
        r for r in task["resource_snapshot"]["knowledge"] if r["id"] != QUALITY_RESOURCE_ID] + [policy]
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if (tx.require("task", task_id) != before_task
                or tx.require("production_session", state["id"]) != before_state):
            raise Conflict("Task changed; reassessment was not applied")
        tx.put("task", task)
        tx.put("production_session", state)
    return {"task_id": task_id, "status": task["status"], "review": receipt,
            "accepted_unchanged": True, "base_unchanged": True, "new_provider_requests": 0}
