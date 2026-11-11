"""One authorized visual replacement, deferred to the normal dependency graph."""
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

from .agent_recovery import replaced_with_authorization
from .tasks import Conflict, TaskService
from ..domain.execution import identity
from ..domain.limits import allows_new_call
from ..persistence.store import now
from ..persistence.transaction import TransactionStore
from ..workflow.graph import SUCCESS, task_status

SCOPE = "one_new_visual_review"
RESULT_FILES = ("answer.json", "result.json", "event-result.json")
FROZEN_FIELDS = (
    "id", "agent_engine", "visual_route", "asset_ids", "target_locales", "source_language",
    "model_snapshot", "resource_snapshot", "image_snapshot", "separation_snapshot",
    "execution_snapshot", "production_policy", "review_draft_authorization", "clean_video_trial",
    "confirmed_briefs", "brief_revision", "production_feedback",
    "max_paid_calls", "max_kie_credits", "max_video_credits", "max_image_calls",
    "max_visual_steps", "max_revisions", "audio_strategy",
)


def _frozen(task):
    return identity({k: task.get(k) for k in FROZEN_FIELDS})


def _ancestors(task, node):
    nodes = {n["id"]: n for n in task["nodes"]}
    found, pending = set(), list(node["depends_on"])
    while pending:
        key = pending.pop()
        if key in found:
            continue
        if key not in nodes or key == node["id"]:
            raise Conflict("Visual dependency graph is invalid")
        found.add(key)
        pending.extend(nodes[key]["depends_on"])
    return found


def _file(service, path, root, *, maximum=None):
    path = Path(path)
    if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
            or not path.is_file() or maximum is not None and path.stat().st_size > maximum):
        raise Conflict("Original visual evidence is missing or outside the task")
    return service.hash_file(path)


def _original_files(service, recovery):
    root = service.settings.runtime / "tasks"
    recovery = Path(recovery)
    if recovery.is_symlink() or not recovery.resolve().is_relative_to(root.resolve()):
        raise Conflict("Visual recovery directory escaped task scope")
    if any((recovery / name).exists() or (recovery / name).is_symlink() for name in RESULT_FILES):
        raise Conflict("A retained original visual result needs reconciliation before replacement")
    files = {name: _file(service, recovery / name, root, maximum=256 * 1024)
             for name in ("request.json", "receipt.json")}
    try:
        request = json.loads((recovery / "request.json").read_bytes())
        receipt = json.loads((recovery / "receipt.json").read_bytes())
        valid = (isinstance(request.get("schema"), dict)
                 and re.fullmatch(r"[0-9a-f]{64}", str(request.get("sha256", "")))
                 and receipt.get("status") == "unknown" and receipt.get("dispatch_possible") is True
                 and receipt.get("reason") in {"worker_interrupted", "cli_nonzero_after_dispatch",
                     "deadline_exceeded", "result_unknown", "missing_final_answer"})
    except (ValueError, AttributeError, UnicodeError):
        valid = False
    if not valid:
        raise Conflict("Original visual request/receipt is not a stopped missing-answer incident")
    return files


def context(service, task_id, expected_decision_id):
    task = service.store.require("task", task_id)
    decision = service.store.require("production_decision", expected_decision_id)
    node = next((n for n in task["nodes"] if n["id"] == decision.get("node_id")), None)
    if (task["status"] != "blocked" or task.get("control_paused") or task.get("agent_engine") != "codex"
            or task.get("visual_route") != "agent_adaptive" or decision.get("task_id") != task_id
            or decision.get("status") != "unknown" or decision.get("key") != "visual-review-0"
            or not node or node["definition_id"] != "visual" or node["status"] != "unknown"
            or node.get("visual_recovery_authorization_id")
            or service.store.get("deletion_receipt", task_id)):
        raise Conflict("A stopped first visual review with no prior replacement is required")
    if any(n["status"] in {"running", "waiting_external"} or n.get("external_pending") for n in task["nodes"]):
        raise Conflict("Wait for all original writers and external requests to stop")
    if any(n["status"] == "unknown" and n["id"] != node["id"] for n in task["nodes"]):
        raise Conflict("Another unknown node requires original-result reconciliation")
    outstanding = {}
    for kind in ("operation", "production_decision", "imagegen_request"):
        outstanding[kind] = [r for r in service.store.list(kind)
            if r.get("task_id") == task_id
            and r.get("status") in {"running", "unknown", "invalid_response", "pending", "claimed"}
            and not replaced_with_authorization(service.store, r, kind)]
    ops, decisions = outstanding["operation"], outstanding["production_decision"]
    traces = [r for r in service.store.list("agent_trace")
              if r.get("task_id") == task_id and r.get("node_id") == node["id"]
              and r.get("decision_id") == decision["id"][:20] and r.get("status") == "unknown"]
    if (outstanding["imagegen_request"] or len(decisions) != 1 or decisions[0] != decision
            or len(ops) != 1 or ops[0].get("node_id") != node["id"]
            or ops[0].get("provider") != "agent" or ops[0].get("kind") != "visual"
            or ops[0].get("status") != "unknown" or ops[0]["created_at"] < decision["created_at"]
            or len(traces) != 1 or traces[0].get("attempt") != node["attempt"]):
        raise Conflict("Cannot bind the sole unresolved visual operation, decision and trace")
    sessions = [s for s in service.store.list("production_session") if s.get("task_id") == task_id
                and any(n["id"] == s["node_id"] and n["definition_id"] == "clean"
                        and n["asset_id"] == node["asset_id"] for n in task["nodes"])]
    if (len(sessions) != 1 or sessions[0].get("candidate")
            or sessions[0].get("pending_turn") is not None
            or sessions[0]["node_id"] not in _ancestors(task, node)):
        raise Conflict("A unique unchanged upstream clean base without pending work is required")
    state = sessions[0]
    root = service.settings.runtime / "tasks" / task_id
    base_sha = _file(service, state["base"]["path"], root)
    if base_sha != state["base"]["sha256"]:
        raise Conflict("Current clean base changed")
    source = service.store.require("asset", node["asset_id"])
    if service.hash_file(service.checked_path(source)) != source["sha256"]:
        raise Conflict("Original source changed")
    recovery = root / "nodes" / node["id"] / str(node["attempt"]) / (decision["id"][:20] + "-codex-recovery")
    files = _original_files(service, recovery)
    if not allows_new_call(task, "max_paid_calls", task.get("paid_calls", 0)):
        raise Conflict("No remaining task allowance for a new review")
    basis = {"task_frozen_sha256": _frozen(task), "nodes_sha256": identity(task["nodes"]),
             "decision_sha256": identity(decision), "operation_sha256": identity(ops[0]),
             "trace_sha256": identity(traces[0]), "session_sha256": identity(state),
             "source_id": source["id"], "source_sha256": source["sha256"], "base_sha256": base_sha,
             "files": files}
    expected = {"expected_decision_id": decision["id"], "expected_base_sha256": base_sha,
                "expected_request_sha256": files["request.json"],
                "expected_receipt_sha256": files["receipt.json"],
                "expected_context_sha256": identity(basis)}
    return {"task_id": task_id, "node_id": node["id"], "expected": expected, "basis": basis,
            "original_operation_id": ops[0]["id"], "original_trace_id": traces[0]["id"],
            "original_recovery_path": str(recovery), "original_attempt": node["attempt"],
            "deferred_until_prerequisites_ready": True, "applied": False, "quality_passed": False}


def authorize(service, task_id, request):
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        scoped = TaskService(service.settings, store)
        fresh = context(scoped, task_id, request.expected_decision_id)
        expected = {k: v for k, v in request.model_dump().items() if k.startswith("expected_")}
        if fresh["expected"] != expected:
            raise Conflict("Visual recovery evidence changed since preview")
        task = store.require("task", task_id)
        node = next(n for n in task["nodes"] if n["id"] == fresh["node_id"])
        auth_id = identity({"scope": SCOPE, "task": task_id, "decision": request.expected_decision_id,
                            "request": request.expected_request_sha256,
                            "receipt": request.expected_receipt_sha256})
        if store.get("agent_recovery_authorization", auth_id):
            raise Conflict("This original visual incident was already authorized")
        authorization = {"id": auth_id, "task_id": task_id, "node_id": node["id"], "status": "applied",
            "scope": SCOPE, "at": now(), "reason": request.reason, "explicit_user_authorization": True,
            "expected": expected, "basis": fresh["basis"],
            "original_recovery_path": fresh["original_recovery_path"], "original_attempt": node["attempt"],
            "original_status_unchanged": True, "cost_status": "unknown", "quality_passed": False,
            "budgets_unchanged": True, "media_generation_authorization_unchanged": True,
            "replacement_key": "visual-review-0-recovery-" + auth_id,
            "replacement_decision_id": None, "deferred_until_prerequisites_ready": True,
            "superseded": {"operation": fresh["original_operation_id"],
                           "production_decision": request.expected_decision_id}}
        node.setdefault("history", []).append({
            "attempt": node["attempt"], "status": node["status"], "error": node.get("error"),
            "authorization_id": auth_id, "reason": "One explicitly authorized new visual review"})
        node.update(status="pending", error=None, finished_at=None, visual_recovery_authorization_id=auth_id)
        task.update(status=task_status(task), updated_at=now())
        store.put("agent_recovery_authorization", authorization)
        for kind, record_id in authorization["superseded"].items():
            store.update(kind, record_id, lambda r: r.update(replacement_authorization_id=auth_id))
        store.put("task", task)
    return {"task_id": task_id, "status": task["status"], "authorization": authorization,
            "scheduled_by_normal_worker_only": True, "quality_passed": False}


def decision_key(store, task, node, key):
    auth_id = node.get("visual_recovery_authorization_id")
    if node["definition_id"] != "visual" or key != "visual-review-0" or not auth_id:
        return key
    auth = store.get("agent_recovery_authorization", auth_id)
    if (not auth or auth.get("scope") != SCOPE or auth.get("status") != "applied"
            or auth.get("task_id") != task["id"] or auth.get("node_id") != node["id"]
            or auth.get("explicit_user_authorization") is not True):
        raise Conflict("Visual replacement authority is missing or revoked")
    return auth["replacement_key"]


def reserve_decision(service, store, task, node, key, decision_id, inputs):
    auth_id = node.get("visual_recovery_authorization_id")
    if node["definition_id"] != "visual" or not auth_id:
        return
    auth = store.require("agent_recovery_authorization", auth_id)
    if key != auth["replacement_key"]:
        return
    current = store.require("task", task["id"])
    fresh_node = next(n for n in current["nodes"] if n["id"] == node["id"])
    if (auth["status"] != "applied" or current.get("control_paused")
            or current["status"] in {"cancelled", "completed"}
            or fresh_node.get("visual_recovery_authorization_id") != auth_id
            or _frozen(current) != auth["basis"]["task_frozen_sha256"]
            or _frozen(task) != auth["basis"]["task_frozen_sha256"]
            or fresh_node["status"] != "running"
            or any(n["status"] not in SUCCESS for n in current["nodes"]
                   if n["id"] in _ancestors(current, fresh_node))):
        raise Conflict("Visual authority, task snapshot or prerequisites changed before dispatch")
    if _original_files(service, auth["original_recovery_path"]) != auth["basis"]["files"]:
        raise Conflict("Original visual request/receipt changed before dispatch")
    source = store.require("asset", fresh_node["asset_id"])
    if (source["id"] != auth["basis"]["source_id"] or source["sha256"] != auth["basis"]["source_sha256"]
            or service.hash_file(service.checked_path(source)) != source["sha256"]):
        raise Conflict("Original source changed before visual dispatch")
    if auth["replacement_decision_id"] not in {None, decision_id}:
        raise Conflict("The one visual replacement was already reserved for another input")
    if auth["replacement_decision_id"] is not None:
        previous = store.get("production_decision", decision_id)
        if not previous or previous["status"] == "failed":
            raise Conflict("The visual replacement intent was consumed; reconcile its original outcome")
    if auth["replacement_decision_id"] is None:
        if not allows_new_call(current, "max_paid_calls", current.get("paid_calls", 0)):
            raise Conflict("Task allowance is exhausted")
        store.update("agent_recovery_authorization", auth_id, lambda a: a.update(
            replacement_decision_id=decision_id, replacement_inputs_sha256=identity(inputs), reserved_at=now()))


def valid_supersession(store, auth):
    """A late original answer stops Route too, before any new upstream Work."""
    if auth.get("explicit_user_authorization") is not True:
        return False
    root = store.path.parent.parent
    expected = (root / "tasks" / auth["task_id"] / "nodes" / auth["node_id"]
                / str(auth["original_attempt"]) / (auth["superseded"]["production_decision"][:20] + "-codex-recovery"))
    if Path(auth["original_recovery_path"]) != expected:
        return False

    def file_sha(path):
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    view = SimpleNamespace(settings=SimpleNamespace(runtime=root), hash_file=file_sha)
    try:
        task = store.require("task", auth["task_id"])
        return (_frozen(task) == auth["basis"]["task_frozen_sha256"]
                and _original_files(view, expected) == auth["basis"]["files"])
    except (Conflict, OSError, KeyError, ValueError):
        return False


def before_model_call(service, task, node, decision_prefix):
    """Recheck after packet/trace creation, immediately before the adapter call."""
    auth_id = node.get("visual_recovery_authorization_id")
    if node["definition_id"] != "visual" or not auth_id:
        return
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        auth = store.require("agent_recovery_authorization", auth_id)
        decision_id = auth.get("replacement_decision_id")
        if decision_id is None or decision_id[:20] != decision_prefix:
            return
        decision = store.require("production_decision", decision_id)
        if decision["status"] != "running":
            raise Conflict("The reserved visual review is no longer dispatchable")
        reserve_decision(service, store, task, node, auth["replacement_key"], decision_id, {})
