"""Consume a verified obsolete accept result without applying pixels or replaying it."""
from copy import deepcopy
import hashlib
import json

from jsonschema import Draft202012Validator, ValidationError

from .agent_recovery import replaced_with_authorization
from .convergence_recovery import _media_evidence
from .retained_clean_checks import _read
from .tasks import Conflict, TaskService
from .visual_recovery import _ancestors
from ..domain.execution import identity
from ..domain.production import CleanAction
from ..persistence.store import now
from ..persistence.transaction import TransactionStore
from ..workflow.graph import SUCCESS, task_status

ERROR = "不能接受不存在或已被替换的候选"
KIND = "stale_clean_decision"
STALE_MESSAGE = "候选已不存在或已被替换；忽略迟到的接受决定，保留当前底片和接受链"


def record_stale(state, action):
    """The caller has appended the original decision to history."""
    state["history"][-1]["tool_error"] = STALE_MESSAGE
    state["history"][-1]["stale_candidate"] = {
        "requested_id": action["candidate_id"],
        "current_id": (state.get("candidate") or {}).get("id"),
        "base_sha256": state["base"]["sha256"],
        "pixels_applied": False,
    }


def _completed(service, task, node, state, decision):
    root = service.settings.runtime / "tasks" / task["id"]
    directory = root / "nodes" / node["id"] / str(node["attempt"])
    prefix = decision["id"][:20]
    files = {}
    packet = _read(service, directory / (prefix + "-work-packet.json"), root, files)
    recovery = directory / (prefix + "-codex-recovery")
    values = {name: _read(service, recovery / (name + ".json"), root, files)
              for name in ("request", "receipt", "event-summary", "event-result", "answer", "result")}
    request, receipt, summary = (values[k] for k in ("request", "receipt", "event-summary"))
    event, raw = values["event-result"], values["answer"]
    answer_sha = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if (receipt.get("status") != "succeeded" or receipt.get("cli_exit_code") != 0
            or receipt.get("event_turn_state") != "completed" or receipt.get("event_stream_invalid")
            or summary.get("event_turn_state") != "completed" or summary.get("event_stream_invalid")
            or event.get("event_turn_state") != "completed" or event.get("result_source") != "completed_event"
            or event.get("answer_sha256") != answer_sha
            or not request.get("sha256") or event.get("request_sha256") != request["sha256"]
            or summary.get("request_sha256") != request["sha256"]
            or raw != values["result"] or raw != event.get("answer")
            or packet.get("task_id") != task["id"]
            or packet.get("node_run_id") != f"{node['id']}:{node['attempt']}"
            or packet.get("decision_id") != prefix or packet.get("stage") != "clean"
            or packet.get("brief_revision", 0) != task.get("brief_revision", 0)
            or packet.get("inputs", {}).get("agent_role") != "work"
            or packet.get("inputs", {}).get("baseline_sha256") != state["base"]["sha256"]
            or packet.get("output_schema") != request.get("schema")):
        raise Conflict("Stale decision lacks matching completed original evidence")
    Draft202012Validator(request["schema"]).validate(raw)
    action = CleanAction.model_validate(raw).model_dump()
    if (action != CleanAction.model_validate(decision.get("result")).model_dump()
            or action["action"] != "accept"
            or action["candidate_id"] == (state.get("candidate") or {}).get("id")):
        raise Conflict("The completed result is not a stale accept")
    traces = [r for r in service.store.list("agent_trace") if r.get("task_id") == task["id"]
              and r.get("node_id") == node["id"] and r.get("attempt") == node["attempt"]
              and r.get("decision_id") == prefix and r.get("agent_role") == "work"]
    if len(traces) != 1 or traces[0]["status"] != "succeeded":
        raise Conflict("Stale decision has no matching successful Work trace")
    return action, files


def _consume(service, task, node):
    store = service.store
    if (task.get("control_paused") or task["status"] != "blocked"
            or task.get("agent_engine") != "codex" or store.get("deletion_receipt", task["id"])
            or any(n["status"] in {"running", "waiting_external", "unknown"} or n.get("external_pending")
                   for n in task["nodes"])
            or any(n["status"] not in SUCCESS for n in task["nodes"] if n["id"] in _ancestors(task, node))):
        return None
    if any(m.get("task_id") == task["id"] and (
            m.get("status") in {"queued", "running"}
            or m.get("created_at", "") > (node.get("finished_at") or ""))
           for m in store.list("conversation")):
        return None
    if any(e.get("task_id") == task["id"] and e.get("status") in {
            "queued", "running", "unknown", "waiting_user"} for e in store.list("task_route_event")):
        return None
    for kind in ("operation", "production_decision", "imagegen_request", "route_run", "route_probe",
                 "task_route_decision"):
        if any(r.get("task_id") == task["id"] and r.get("status") in {
                "running", "unknown", "invalid_response", "pending", "claimed", "verifying"}
               and not replaced_with_authorization(store, r, kind) for r in store.list(kind)):
            return None
    sessions = [r for r in store.list("production_session")
                if r.get("task_id") == task["id"] and r.get("node_id") == node["id"]]
    if len(sessions) != 1:
        return None
    state = sessions[0]
    if (state.get("pending_turn") is not None or state.get("finished") or state.get("draft_handoff")
            or state.get("retained_check") or state.get("archive_review") or state.get("pipeline_review")
            or any(h["turn"] == state["turn"] for h in state.get("history", []))):
        return None
    decisions = [d for d in store.list("production_decision") if d.get("task_id") == task["id"]
                 and d.get("node_id") == node["id"] and d.get("key") == f"clean-{state['turn']}"]
    if len(decisions) != 1 or decisions[0]["status"] != "succeeded":
        return None
    decision = decisions[0]
    key = identity({"task": task["id"], "decision": decision["id"], "protocol": 1})
    if store.get(KIND, key):
        return None
    action, files = _completed(service, task, node, state, decision)
    asset = store.require("asset", node["asset_id"])
    source = service.checked_path(asset)
    if node["asset_id"] not in task["asset_ids"] or service.hash_file(source) != asset["sha256"]:
        raise Conflict("Stale decision source changed")
    files.update(_media_evidence(service, task, state, source))
    if state.get("candidate"):
        files.update(_media_evidence(service, task, {"base": state["candidate"]}, source))
    before = identity(state)
    state["history"].append({"turn": state["turn"], "decision": action,
                             "input_base_sha256": state["base"]["sha256"], "recovery_id": key})
    record_stale(state, action)
    record = {"id": key, "task_id": task["id"], "node_id": node["id"], "status": "applied",
              "original_attempt": node["attempt"], "original_error": node["error"],
              "original_decision": deepcopy(decision), "session_id": state["id"], "turn": state["turn"],
              "state_before_sha256": before, "files": files, "created_at": now(),
              "external_calls": 0, "pixels_applied": False, "quality_passed": False}
    state["turn"] += 1
    node.setdefault("history", []).append({
        "attempt": node["attempt"], "status": node["status"], "error": node["error"],
        "reason": "Verified stale accept consumed without applying pixels", "recovery_id": key})
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    store.put(KIND, record)
    store.put("production_session", state)
    store.put("task", task)
    return record


def reconcile(service, task_id):
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        task = store.require("task", task_id)
        node = next((n for n in task["nodes"] if n["definition_id"] == "clean"
                     and n["status"] == "failed" and n.get("error") == ERROR), None)
        if not node:
            return None
        try:
            return _consume(TaskService(service.settings, store), task, node)
        except (Conflict, OSError, KeyError, ValueError, ValidationError):
            return None
