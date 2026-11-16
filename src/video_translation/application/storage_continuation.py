"""Return locally stopped storage work to the original production queue."""
from .agent_recovery import replaced_with_authorization
from .convergence_recovery import _media_evidence
from .tasks import Conflict
from .visual_recovery import _ancestors
from ..domain.execution import identity
from ..execution.resource_budget import guard_storage
from ..persistence.store import now
from ..workflow.graph import SUCCESS, task_status

KIND = "storage_continuation"
HISTORICAL_ERRORS = {
    "Task workspace file allowance reached or inventory is incomplete",
    "Task workspace byte allowance reached; retain current results",
}


def storage_error(error):
    value = str(error or "").removeprefix("Task resource storage block: ")
    return value in HISTORICAL_ERRORS or value.startswith("Insufficient workspace space:")


def schedule(service, task):
    """Called under the Manager task guard and transaction; no production tool calls."""
    store = service.store
    policy = store.require("route_policy", task["id"])
    node = next((n for n in task["nodes"] if n["id"] == policy["node_id"]
                 and n["definition_id"] == "clean" and n["status"] == "failed"
                 and storage_error(n.get("error"))), None)
    if not node:
        return False
    if (not policy.get("enabled") or not policy.get("recover_production_route")
            or task.get("control_paused") or task["status"] != "blocked"
            or store.get("deletion_receipt", task["id"])
            or service.collaboration.pending(task["id"])):
        raise Conflict("Storage continuation waits for current task control and messages")
    if (any(n["status"] in {"running", "waiting_external", "unknown"} or n.get("external_pending")
            for n in task["nodes"])
            or any(n["status"] not in SUCCESS for n in task["nodes"]
                   if n["id"] in _ancestors(task, node))):
        raise Conflict("Storage continuation waits for original production and prerequisites")
    # Historical technical diagnostics do not produce media. Their unknown costs
    # stay untouched; only real production requests can block this local handback.
    for kind in ("operation", "production_decision", "imagegen_request"):
        if any(r.get("task_id") == task["id"] and r.get("status") in {
                "running", "unknown", "invalid_response", "pending", "claimed"}
               and not replaced_with_authorization(store, r, kind) for r in store.list(kind)):
            raise Conflict("Original production requests require reconciliation before storage continuation")
    sessions = [s for s in store.list("production_session")
                if s.get("task_id") == task["id"] and s.get("node_id") == node["id"]]
    if len(sessions) != 1 or sessions[0].get("finished"):
        raise Conflict("A unique unfinished original session is required")
    state = sessions[0]
    if state.get("pending_turn") is not None:
        saved = [d for d in store.list("production_decision")
                 if d.get("task_id") == task["id"] and d.get("node_id") == node["id"]
                 and d.get("key") == f"clean-{state['turn']}" and d.get("status") == "succeeded"]
        if state["pending_turn"] != state["turn"] or len(saved) != 1:
            raise Conflict("Pending production must retain its unique successful original decision")
    source_record = store.require("asset", node["asset_id"])
    source = service.checked_path(source_record)
    if (node["asset_id"] not in task["asset_ids"]
            or service.hash_file(source) != source_record["sha256"]):
        raise Conflict("Original storage-incident source changed")
    files = _media_evidence(service, task, state, source)
    candidate = state.get("candidate")
    if candidate:
        if candidate.get("input_sha256") != state["base"]["sha256"]:
            raise Conflict("The retained candidate belongs to a different base")
        files.update(_media_evidence(service, task, {"base": candidate}, source))
    guard_storage(service, task)
    key = identity({"task": task["id"], "node": node["id"], "attempt": node["attempt"],
                    "error": node["error"], "session": identity(state)})
    if store.get(KIND, key):
        raise Conflict("This storage incident has already returned to the Worker")
    store.put(KIND, {
        "id": key, "task_id": task["id"], "node_id": node["id"], "status": "scheduled",
        "created_at": now(), "delegation_id": policy["authorization_id"],
        "original_attempt": node["attempt"], "original_error": node["error"],
        "session_id": state["id"], "session_sha256": identity(state), "files": files,
        "workspace_quotas_enforced": False, "external_calls": 0,
        "allowance_reset": False, "quality_passed": False,
    })
    node.update(status="pending", error=None, finished_at=None, storage_continuation_id=key)
    task.update(status=task_status(task), updated_at=now())
    store.put("task", task)
    return True
