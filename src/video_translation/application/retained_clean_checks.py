"""Reconcile completed read-only checks after known local contract faults."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from .agent_recovery import replaced_with_authorization
from .convergence_recovery import _media_evidence
from .tasks import Conflict, TaskService
from .visual_recovery import _ancestors, _frozen
from ..domain.execution import identity
from ..domain.production import CleanAction
from ..persistence.store import now
from ..persistence.transaction import TransactionStore
from ..workflow.graph import SUCCESS, task_status

KIND = "retained_clean_check"


def _binding(task):
    return identity({"frozen": _frozen(task), "directions": task.get("collaboration_rules"),
                     "notes": task.get("notes"), "paid_calls": task.get("paid_calls")})


def _state_binding(state):
    return identity({k: state.get(k) for k in (
        "id", "node_id", "base", "candidate", "accepted", "turn", "text_overrides", "history",
        "times_ms", "pipeline_review", "pipeline_times_ms")})


def _messages(store, task_id):
    return sorted(m["id"] for m in store.list("conversation") if m.get("task_id") == task_id)


def _read(service, path, root, files, maximum=4 * 1024**2):
    path = Path(path)
    if (not path.resolve().is_relative_to(root.resolve()) or not path.is_file()
            or path.stat().st_size > maximum or any(
                p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 1024
                for p in (path, *path.parents))):
        raise Conflict("Retained check evidence escaped its task or changed")
    files[str(path)] = service.hash_file(path)
    return json.loads(path.read_text("utf-8"))


def _completed(service, task, node, state, decision):
    root = service.settings.runtime / "tasks" / task["id"]
    directory = root / "nodes" / node["id"] / str(node["attempt"])
    prefix = decision["id"][:20]
    recovery = directory / (prefix + "-codex-recovery")
    files = {}
    packet = _read(service, directory / (prefix + "-work-packet.json"), root, files)
    values = {name: _read(service, recovery / (name + ".json"), root, files)
              for name in ("request", "receipt", "event-summary", "event-result", "answer", "result")}
    request, receipt = values["request"], values["receipt"]
    event, summary, raw = values["event-result"], values["event-summary"], values["answer"]
    canonical_sha = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if (receipt.get("status") != "succeeded" or receipt.get("cli_exit_code") != 0
            or receipt.get("event_turn_state") != "completed" or receipt.get("event_stream_invalid")
            or summary.get("event_turn_state") != "completed" or summary.get("event_stream_invalid")
            or event.get("event_turn_state") != "completed"
            or event.get("result_source") != "completed_event"
            or event.get("answer_sha256") != canonical_sha
            or event.get("request_sha256") != request.get("sha256")
            or summary.get("request_sha256") != request.get("sha256")
            or raw != values["result"] or raw != event.get("answer")
            or packet.get("task_id") != task["id"]
            or packet.get("node_run_id") != f"{node['id']}:{node['attempt']}"
            or packet.get("decision_id") != prefix or packet.get("stage") != "clean"
            or packet.get("brief_revision", 0) != task.get("brief_revision", 0)
            or packet.get("inputs", {}).get("agent_role") != "work"
            or packet.get("inputs", {}).get("baseline_sha256") != state["base"]["sha256"]
            or packet.get("output_schema") != request.get("schema")):
        raise Conflict("Original check has no matching completed result evidence")
    original_candidate = packet["inputs"].get("candidate") or {}
    if any(original_candidate.get(k) != state["candidate"].get(k)
           for k in ("id", "sha256", "input_sha256")):
        raise Conflict("The candidate changed since the completed check")
    Draft202012Validator(request["schema"]).validate(raw)
    action = CleanAction.model_validate(raw).model_dump()
    if action["candidate_id"] != state["candidate"]["id"]:
        raise Conflict("Read-only check does not bind the current candidate")
    if node["error"] == "'frame_index'":
        if (decision["status"] != "succeeded" or action["action"] != "inspect"
                or action != CleanAction.model_validate(decision.get("result")).model_dump()
                or state.get("pipeline_review") != state["candidate"]["id"]
                or state["candidate"].get("pipeline", {}).get("media_type") != "still_image"):
            raise Conflict("Not the retained still-pipeline reporting fault")
    elif (decision["status"] != "invalid_response" or action["action"] != "inspect_metadata"
          or raw.get("action") != "inspect" or raw.get("times_ms") != []):
        raise Conflict("Not the completed candidate metadata contract mismatch")
    traces = [t for t in service.store.list("agent_trace") if t.get("task_id") == task["id"]
              and t.get("node_id") == node["id"] and t.get("decision_id") == prefix
              and t.get("attempt") == node["attempt"] and t.get("agent_role") == "work"]
    expected = "succeeded" if decision["status"] == "succeeded" else "failed"
    if len(traces) != 1 or traces[0]["status"] != expected:
        raise Conflict("Original Work trace does not match the completed local check")
    return action, files, len(packet.get("images", []))


def _schedule(service, task, node):
    store = service.store
    if (task.get("control_paused") or task["status"] != "blocked"
            or task.get("agent_engine") != "codex" or task.get("output_goal") != "complete_candidate"
            or store.get("deletion_receipt", task["id"])
            or any(n["status"] in {"running", "waiting_external", "unknown"} or n.get("external_pending")
                   for n in task["nodes"])
            or any(n["status"] not in SUCCESS for n in task["nodes"] if n["id"] in _ancestors(task, node))):
        return None
    if any(m.get("task_id") == task["id"] and (
            m.get("status") in {"queued", "running"}
            or m.get("created_at", "") > (node.get("finished_at") or ""))
           for m in store.list("conversation")):
        return None
    if any(e.get("task_id") == task["id"] and e.get("status") in {"queued", "running", "unknown", "waiting_user"}
           for e in store.list("task_route_event")):
        return None
    sessions = [s for s in store.list("production_session")
                if s.get("task_id") == task["id"] and s.get("node_id") == node["id"]]
    if len(sessions) != 1:
        return None
    state = sessions[0]
    if (not state.get("candidate") or state.get("pending_turn") is not None
            or state.get("finished") or state.get("draft_handoff") or state.get("retained_check")
            or state.get("archive_review") or any(h["turn"] == state["turn"] for h in state.get("history", []))
            or state["candidate"].get("input_sha256") != state["base"]["sha256"]):
        return None
    decisions = [d for d in store.list("production_decision")
                 if d.get("task_id") == task["id"] and d.get("node_id") == node["id"]
                 and d.get("key") == f"clean-{state['turn']}"]
    if len(decisions) != 1:
        return None
    decision = decisions[0]
    key = identity({"task": task["id"], "node": node["id"], "decision": decision["id"], "protocol": 1})
    if store.get(KIND, key):
        return None
    for kind in ("operation", "production_decision", "imagegen_request", "route_run", "route_probe",
                 "task_route_decision"):
        if any(r.get("task_id") == task["id"]
               and r.get("status") in {"running", "unknown", "invalid_response", "pending", "claimed", "verifying"}
               and not (kind == "production_decision" and r["id"] == decision["id"]
                        and r["status"] == "invalid_response")
               and not replaced_with_authorization(store, r, kind) for r in store.list(kind)):
            return None
    action, files, images_sent = _completed(service, task, node, state, decision)
    asset = store.require("asset", node["asset_id"])
    source = service.checked_path(asset)
    if node["asset_id"] not in task["asset_ids"] or service.hash_file(source) != asset["sha256"]:
        raise Conflict("Original check source changed")
    files.update(_media_evidence(service, task, state, source))
    files.update(_media_evidence(service, task, {**state, "base": state["candidate"]}, source))
    files[str(source)] = asset["sha256"]
    record = {"id": key, "task_id": task["id"], "node_id": node["id"], "status": "scheduled",
              "original_attempt": node["attempt"], "original_error": node["error"],
              "original_decision": deepcopy(decision), "session_id": state["id"], "turn": state["turn"],
              "base_sha256": state["base"]["sha256"], "candidate_sha256": state["candidate"]["sha256"],
              "binding": _binding(task), "files": files, "created_at": now(),
              "state_binding": _state_binding(state), "message_ids": _messages(store, task["id"]),
              "external_calls": 0, "quality_passed": False}
    state["retained_check"] = {"id": key, "decision_id": decision["id"], "turn": state["turn"]}
    if decision["status"] == "invalid_response":
        decision.update(status="succeeded", result=action, retained_check_recovery_id=key,
                        media={"mode": "images_supplied_to_command", "images_sent": images_sent,
                               "consumption_verified": False}, finished_at=now())
        store.put("production_decision", decision)
    store.put(KIND, record)
    store.put("production_session", state)
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    store.put("task", task)
    return record


def reconcile(service, task_id):
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        task = store.require("task", task_id)
        node = next((n for n in task["nodes"] if n["definition_id"] == "clean"
                     and n["status"] == "failed" and (n.get("error") == "'frame_index'"
                     or "检查必须指定实际时间点" in (n.get("error") or ""))), None)
        if not node:
            return None
        try:
            return _schedule(TaskService(service.settings, store), task, node)
        except (Conflict, OSError, KeyError, ValueError, ValidationError):
            return None


def verify_replay(controller, state):
    marker = state.get("retained_check")
    if not marker:
        return
    current = controller.store.require("task", controller.task["id"])
    record = controller.store.require(KIND, marker["id"])
    if (record["status"] != "scheduled" or marker["turn"] != state["turn"]
            or record["session_id"] != state["id"] or _binding(current) != record["binding"]
            or _state_binding(state) != record["state_binding"]
            or _messages(controller.store, current["id"]) != record["message_ids"]
            or current.get("control_paused") or current["status"] in {"cancelled", "completed"}
            or state["base"]["sha256"] != record["base_sha256"]
            or (state.get("candidate") or {}).get("sha256") != record["candidate_sha256"]
            or controller.store.get("deletion_receipt", current["id"])):
        raise Conflict("Retained check recovery changed; preserve the original candidate")
    for path, expected in record["files"].items():
        path = Path(path)
        if (any(p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 1024
                for p in (path, *path.parents)) or controller.service.hash_file(path) != expected):
            raise Conflict("Retained check evidence changed before replay")


def finish_replay(controller, state, turn):
    marker = state.get("retained_check")
    if not marker:
        return False
    if marker["turn"] != turn:
        raise Conflict("Retained check turn changed")
    state.pop("retained_check")
    with controller.store.connection() as db:
        store = TransactionStore(controller.store, db)
        store.put("production_session", state)
        store.update(KIND, marker["id"], lambda r: r.update(
            status="applied", finished_at=now(), final_turn=state["turn"]))
    return True
