"""Manager queues reviewed cleanup after a verified source-design refinement."""
from .agent_recovery import replaced_with_authorization
from .convergence_recovery import _media_evidence
from .tasks import Conflict
from .visual_recovery import _ancestors
from ..domain.execution import identity
from ..execution.clean_design_binding import ERROR, SETTLED_ERROR, checked_rebinding
from ..persistence.store import now
from ..workflow.graph import SUCCESS, task_status

KIND = "clean_design_continuation"


def schedule(service, task):
    store = service.store
    policy = store.require("route_policy", task["id"])
    node = next((n for n in task["nodes"] if n["id"] == policy["node_id"]
                 and n["definition_id"] == "clean" and n["status"] == "failed"
                 and n.get("error") in {ERROR, SETTLED_ERROR}), None)
    if not node:
        return False
    if (not policy.get("enabled") or not policy.get("recover_production_route")
            or task.get("control_paused") or task["status"] != "blocked"
            or store.get("deletion_receipt", task["id"])
            or service.collaboration.pending(task["id"])):
        raise Conflict("Source design continuation waits for task control and messages")
    if (any(n["status"] in {"running", "waiting_external", "unknown"} or n.get("external_pending")
            for n in task["nodes"]) or any(n["status"] not in SUCCESS for n in task["nodes"]
                                        if n["id"] in _ancestors(task, node))):
        raise Conflict("Source design continuation waits for original production")
    for kind in ("operation", "production_decision", "imagegen_request"):
        if any(r.get("task_id") == task["id"] and r.get("status") in {
                "running", "unknown", "invalid_response", "pending", "claimed"}
               and not replaced_with_authorization(store, r, kind) for r in store.list(kind)):
            raise Conflict("Original production requests require reconciliation")
    sessions = [s for s in store.list("production_session")
                if s.get("task_id") == task["id"] and s.get("node_id") == node["id"]]
    if len(sessions) != 1:
        raise Conflict("A unique original clean session is required")
    state = sessions[0]
    visual = next(n for n in task["nodes"] if n["definition_id"] == "visual_analyze"
                  and n["asset_id"] == node["asset_id"])
    proof = checked_rebinding(service, task, node, state, visual["output"])
    if proof is None:
        if state.get("candidate") or state.get("pending_turn") is not None:
            raise Conflict("Original candidate or pending turn requires reconciliation")
        proof = {"session_id": state["id"], "design_sha256": identity(visual["output"]),
                 "source_sha256": service.store.require("asset", node["asset_id"])["sha256"],
                 "base_sha256": state["base"]["sha256"], "retained_design_binding": True,
                 "quality_acceptance": False}
    source = service.checked_path(store.require("asset", node["asset_id"]))
    files = _media_evidence(service, task, state, source)
    files[str(source)] = proof["source_sha256"]
    key = identity({"task": task["id"], "node": node["id"], "attempt": node["attempt"],
                    "proof": proof})
    if store.get(KIND, key):
        raise Conflict("This source design continuation was already scheduled")
    store.put(KIND, {
        "id": key, "task_id": task["id"], "node_id": node["id"], "status": "scheduled",
        "created_at": now(), "original_attempt": node["attempt"],
        "original_error": node["error"], "session_sha256": identity(state),
        "proof": proof, "files": files, "external_calls": 0,
        "quality_acceptance": False, "allowance_reset": False,
    })
    node.update(status="pending", error=None, finished_at=None, clean_design_continuation_id=key)
    task.update(status=task_status(task), updated_at=now())
    store.put("task", task)
    return True
