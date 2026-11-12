"""One delegated fresh planning decision after a stopped, unconfirmed planner."""
import hashlib
import json
from pathlib import Path

from .agent_recovery import replaced_with_authorization
from .clean_base import verified_clean_base
from .clean_route_incident import _read
from .clean_route_session import matches as session_matches
from .tasks import Conflict
from .visual_recovery import _ancestors, _frozen
from ..domain.execution import identity
from ..domain.limits import allows_new_call
from ..domain.production import CleanRouteDecision
from ..persistence.store import now
from ..workflow.graph import SUCCESS, task_status

SCOPE = "one_new_clean_route"
SESSION_ERROR = "Planner session changed before dispatch"


def original_files(root, recovery):
    """Bind even untrusted output; its existence never certifies success."""
    files = {}
    for name in ("result.json", "event-result.json"):
        path = recovery / name
        if path.exists() or path.is_symlink():
            raise Conflict("A completed result appeared; reconcile it before a new plan")
    for name in ("request.json", "receipt.json", "answer.json"):
        path = recovery / name
        if name == "answer.json" and not path.exists() and not path.is_symlink():
            files[str(path)] = None
        else:
            files[str(path)] = hashlib.sha256(_read(path, root, 2 * 1024**2)).hexdigest()
    packet = recovery.parent / (recovery.name[:20] + "-work-packet.json")
    files[str(packet)] = hashlib.sha256(_read(packet, root, 4 * 1024**2)).hexdigest()
    return files


def schedule(service, task, incident):
    """Called by the project manager under its task guard and database transaction."""
    store = service.store
    policy = store.require("route_policy", task["id"])
    if (not policy.get("enabled") or not policy.get("recover_production_route")
            or task.get("control_paused") or task["status"] != "blocked"
            or store.get("deletion_receipt", task["id"])):
        raise Conflict("Planner recovery is not delegated or task control changed")
    decision = store.require("production_decision", incident["original_decision_id"])
    node = next(n for n in task["nodes"] if n["id"] == decision["node_id"])
    sessions = [s for s in store.list("production_session")
                if s.get("task_id") == task["id"] and s["node_id"] == node["id"]]
    if (node["status"] != "unknown" or node.get("external_pending") or len(sessions) != 1
            or node["id"] != policy["node_id"] or decision["status"] != "unknown"
            or decision.get("replacement_authorization_id")):
        raise Conflict("The original planner is no longer uniquely stopped")
    state = sessions[0]
    if (state.get("candidate") or state.get("pending_turn") is not None or state.get("finished")
            or any(n["status"] in {"running", "waiting_external"} or n.get("external_pending")
                   or n["status"] == "unknown" and n["id"] != node["id"] for n in task["nodes"])
            or any(n["status"] not in SUCCESS for n in task["nodes"]
                   if n["id"] in _ancestors(task, node))):
        raise Conflict("Pending production or prerequisites require reconciliation")
    outstanding = {}
    for kind in ("production_decision", "operation", "imagegen_request"):
        outstanding[kind] = [r for r in store.list(kind)
            if r.get("task_id") == task["id"] and r.get("status") in {
                "running", "unknown", "invalid_response", "pending", "claimed"}
            and not replaced_with_authorization(store, r, kind)]
    operations = outstanding["operation"]
    traces = [t for t in store.list("agent_trace") if t.get("task_id") == task["id"]
              and t.get("node_id") == node["id"] and t.get("decision_id") == decision["id"][:20]]
    if (outstanding["production_decision"] != [decision] or outstanding["imagegen_request"]
            or len(operations) != 1 or operations[0].get("provider") != "agent"
            or operations[0].get("node_id") != node["id"] or operations[0].get("kind") != "clean"
            or operations[0]["status"] != "unknown"
            or operations[0]["created_at"] < decision["created_at"] or len(traces) != 1
            or traces[0].get("agent_role") != "route" or traces[0]["status"] != "unknown"
            or traces[0].get("attempt") != node["attempt"]):
        raise Conflict("Cannot bind a single stopped planning call")
    if any(a.get("task_id") == task["id"] and a.get("node_id") == node["id"]
           and a.get("scope") == SCOPE for a in store.list("agent_recovery_authorization")):
        raise Conflict("Planner recovery was already used; inspect the new bridge evidence")
    root = service.settings.runtime / "tasks" / task["id"]
    recovery = root / "nodes" / node["id"] / str(node["attempt"]) / (
        decision["id"][:20] + "-codex-recovery")
    files = original_files(root, recovery)
    request = json.loads(_read(recovery / "request.json", root, 256 * 1024))
    receipt = json.loads(_read(recovery / "receipt.json", root, 16 * 1024))
    packet = json.loads(_read(recovery.parent / (
        decision["id"][:20] + "-work-packet.json"), root, 4 * 1024**2))
    if (request.get("schema") != CleanRouteDecision.model_json_schema()
            or packet.get("output_schema") != request["schema"]
            or packet.get("inputs", {}).get("agent_role") != "route"
            or packet.get("images") or packet.get("decision_id") != decision["id"][:20]
            or receipt.get("status") != "unknown" or receipt.get("dispatch_possible") is not True
            or receipt.get("cli_exit_code") != 0 or receipt.get("event_stream_invalid") is not True
            or receipt.get("event_turn_state") != "invalid" or receipt.get("reason") != "result_unknown"):
        raise Conflict("Not a stopped structured planner with invalid completion evidence")
    from ..execution.resource_budget import guard_external_call, guard_recovery_call, guard_storage
    guard_external_call(task)
    guard_recovery_call(store, task)
    guard_storage(service, task)
    if not allows_new_call(task, "max_paid_calls", task.get("paid_calls", 0)):
        raise Conflict("No remaining call allowance for a new plan")
    verified_clean_base(service, task, state, state["base"]["sha256"])
    source_record = store.require("asset", node["asset_id"])
    if source_record["id"] not in task["asset_ids"]:
        raise Conflict("Planner source no longer belongs to this task")
    source = service.checked_path(source_record)
    if service.hash_file(source) != source_record["sha256"]:
        raise Conflict("Registered source changed")
    from .convergence_recovery import _media_evidence
    media = _media_evidence(service, task, state, source)
    media[str(source)] = source_record["sha256"]
    auth_id = identity({"scope": SCOPE, "decision": decision["id"], "files": files})
    authorization = {
        "id": auth_id, "scope": SCOPE, "task_id": task["id"], "node_id": node["id"],
        "status": "applied", "at": now(), "authority": "delegated_project_manager",
        "delegation_id": policy["authorization_id"], "original_key": decision["key"],
        "replacement_key": decision["key"] + "-recovery-" + auth_id[:20],
        "replacement_decision_id": None, "original_recovery_path": str(recovery),
        "original_attempt": node["attempt"], "original_files": files, "media_files": media,
        "session_id": state["id"], "session_sha256": identity(state),
        "task_frozen_sha256": _frozen(task),
        "superseded": {"production_decision": decision["id"], "operation": operations[0]["id"]},
        "original_status_unchanged": True, "cost_status": "unknown",
        "retained_answer_adopted": False, "quality_passed": False,
    }
    store.put("agent_recovery_authorization", authorization)
    for kind, record_id in authorization["superseded"].items():
        store.update(kind, record_id, lambda r: r.update(replacement_authorization_id=auth_id))
    node.update(status="pending", error=None, finished_at=None, clean_route_recovery_id=auth_id)
    task.update(status=task_status(task), updated_at=now())
    store.put("task", task)
    return authorization


def valid_supersession(store, auth):
    try:
        policy = store.require("route_policy", auth["task_id"])
        if (not policy.get("enabled") or not policy.get("recover_production_route")
                or policy["authorization_id"] != auth["delegation_id"]):
            return False
        runtime = store.path.parent.parent
        root = runtime / "tasks" / auth["task_id"]
        recovery = root / "nodes" / auth["node_id"] / str(auth["original_attempt"]) / (
            auth["superseded"]["production_decision"][:20] + "-codex-recovery")
        return (str(recovery) == auth["original_recovery_path"]
                and original_files(root, recovery) == auth["original_files"])
    except (Conflict, KeyError, OSError, ValueError):
        return False


def decision_key(store, task, node, key):
    auth_id = node.get("clean_route_recovery_id")
    if not auth_id:
        return key
    auth = store.require("agent_recovery_authorization", auth_id)
    return auth["replacement_key"] if key == auth["original_key"] else key


def _check_dispatch(service, store, task, node, auth, *, status):
    auth_id = auth["id"]
    current = store.require("task", task["id"])
    current_node = next(n for n in current["nodes"] if n["id"] == node["id"])
    policy = store.require("route_policy", task["id"])
    if (auth["scope"] != SCOPE or auth["status"] != "applied"
            or auth["task_id"] != task["id"] or auth["node_id"] != node["id"]
            or current.get("control_paused") or current["status"] in {"cancelled", "completed"}
            or current_node.get("clean_route_recovery_id") != auth_id
            or current_node["status"] != status or current_node.get("external_pending")
            or store.get("deletion_receipt", task["id"])
            or not policy["enabled"] or not policy.get("recover_production_route")
            or policy["authorization_id"] != auth["delegation_id"]
            or _frozen(current) != auth["task_frozen_sha256"]
            or not valid_supersession(store, auth)):
        raise Conflict("Planner recovery authority or original evidence changed")
    from ..execution.resource_budget import guard_external_call
    # paid() may already have reserved this call; its normal admission gate owns the limit there.
    if auth["replacement_decision_id"] is None:
        guard_external_call(current)
        if not allows_new_call(current, "max_paid_calls", current.get("paid_calls", 0)):
            raise Conflict("No remaining call allowance")
    for path, sha in auth["media_files"].items():
        media_path = Path(path)
        if (not media_path.resolve().is_relative_to(service.settings.runtime.resolve())
                or any(p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 1024
                       for p in (media_path, *media_path.parents))
                or service.hash_file(media_path) != sha):
            raise Conflict("Planner base dependencies changed")
    if not session_matches(store.require("production_session", auth["session_id"]),
                           auth["session_sha256"]):
        raise Conflict(SESSION_ERROR)


def reserve_decision(service, store, task, node, key, decision_id, inputs):
    auth_id = node.get("clean_route_recovery_id")
    if not auth_id:
        return
    auth = store.require("agent_recovery_authorization", auth_id)
    if key != auth["replacement_key"]:
        return
    if inputs.get("agent_role") != "route":
        raise Conflict("Planner recovery requires a Route decision")
    _check_dispatch(service, store, task, node, auth, status="running")
    if auth["replacement_decision_id"] not in {None, decision_id}:
        raise Conflict("The one new plan was already reserved")
    if auth["replacement_decision_id"] is not None:
        previous = store.get("production_decision", decision_id)
        if not previous or previous["status"] == "failed":
            raise Conflict("The new planning intent was consumed; reconcile its outcome")
    if auth["replacement_decision_id"] is None:
        store.update("agent_recovery_authorization", auth_id,
                     lambda a: a.update(replacement_decision_id=decision_id, reserved_at=now()))


def before_model_call(service, task, node, decision_prefix, inputs):
    if not node.get("clean_route_recovery_id"):
        return
    from ..persistence.transaction import TransactionStore
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        auth = store.require("agent_recovery_authorization", node["clean_route_recovery_id"])
        decision_id = auth["replacement_decision_id"]
        if decision_id and decision_id[:20] == decision_prefix:
            reserve_decision(service, store, task, node, auth["replacement_key"],
                             decision_id, inputs)


def requeue_predispatch(service, task):
    """Manager resumes the same unreserved intent once, never a dispatched call."""
    store = service.store
    node = next((n for n in task["nodes"] if n["definition_id"] == "clean"
                 and n["status"] == "failed" and n.get("error") == SESSION_ERROR), None)
    if not node or not node.get("clean_route_recovery_id"):
        return False
    auth = store.require("agent_recovery_authorization", node["clean_route_recovery_id"])
    if (auth.get("replacement_decision_id") is not None or auth.get("predispatch_reentry")
            or node["attempt"] != auth["original_attempt"] + 1
            or task["status"] != "blocked" or service.collaboration.pending(task["id"])):
        raise Conflict("Local planning intent was consumed or user intent changed")
    if any(n["status"] in {"running", "waiting_external", "unknown"} or n.get("external_pending")
           for n in task["nodes"] if n["id"] != node["id"]):
        raise Conflict("Other production requires reconciliation")
    if any(n["status"] not in SUCCESS for n in task["nodes"]
           if n["id"] in _ancestors(task, node)):
        raise Conflict("Planner prerequisites changed")
    original_decision = auth["superseded"]["production_decision"]
    for kind in ("production_decision", "operation", "agent_trace",
                 "imagegen_request", "route_run", "route_probe"):
        for row in store.list(kind):
            if row.get("task_id") != task["id"]:
                continue
            original = (row["id"] == auth["superseded"].get(kind)
                        or kind == "agent_trace" and row.get("decision_id") == original_decision[:20])
            if original:
                if row.get("status") != "unknown":
                    raise Conflict("Original planning outcome changed")
                continue
            timestamp = row.get("created_at")
            superseded = replaced_with_authorization(store, row, kind)
            if kind == "agent_trace" and row.get("decision_id"):
                decisions = [d for d in store.list("production_decision")
                             if d.get("task_id") == task["id"]
                             and d["id"][:20] == row["decision_id"]]
                superseded = (len(decisions) == 1 and replaced_with_authorization(
                    store, decisions[0], "production_decision"))
            if (not timestamp or timestamp >= auth["at"]
                    or row.get("key") == auth["replacement_key"]
                    or row.get("status") in {"running", "pending", "claimed"}
                    or row.get("status") in {"unknown", "invalid_response"} and not superseded):
                raise Conflict("New or unsettled dispatch evidence prevents local reentry")
    directory = (service.settings.runtime / "tasks" / task["id"] / "nodes"
                 / node["id"] / str(node["attempt"]))
    if directory.is_symlink() or any(p.is_file() or p.is_symlink() for p in directory.rglob("*")):
        raise Conflict("Local attempt has retained output; reconcile before reentry")
    _check_dispatch(service, store, task, node, auth, status="failed")
    from ..execution.resource_budget import guard_recovery_call, guard_storage
    guard_recovery_call(store, task)
    guard_storage(service, task)
    state = store.require("production_session", auth["session_id"])
    store.update("agent_recovery_authorization", auth["id"], lambda a: a.update(
        predispatch_reentry={"at": now(), "failed_attempt": node["attempt"],
                            "error": node["error"], "session_sha256": identity(state),
                            "meaning": "Same unreserved planning intent; no provider replay"}))
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    store.put("task", task)
    return True
