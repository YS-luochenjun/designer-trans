"""Queue only a failed local observation tail; never execute inference or media."""
from copy import deepcopy
import json
from pathlib import Path
import stat

from jsonschema import Draft202012Validator

from ..execution.adaptive_observe import ObservationDecision, _digest, observation_binding
from ..persistence.store import now
from ..persistence.transaction import TransactionStore
from .collaboration import instructions_for
from .tasks import Conflict


ERROR = "Inspection time is outside original source"
LIMIT = 6


def _file(service, root, path, files, expected=None):
    path = Path(path)
    if not path.is_absolute() or not path.is_relative_to(root):
        raise Conflict("Observation evidence escaped its original directory")
    for item in (path, *path.parents):
        if item == root.parent:
            break
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Conflict("Linked observation evidence is not supported")
    if not path.is_file():
        raise Conflict("Original observation evidence is missing")
    actual = service.hash_file(path)
    if expected is not None and expected != actual:
        raise Conflict("Original observation evidence changed")
    files[str(path)] = actual
    return path


def _json(service, root, path, files, expected=None):
    path = _file(service, root, path, files, expected)
    if path.stat().st_size > 2 * 1024**2:
        raise Conflict("Observation JSON evidence exceeds its contract")
    return json.loads(path.read_text("utf-8"))


def _context(service, store, task_id):
    task = store.require("task", task_id)
    if (task["status"] != "blocked" or task.get("control_paused")
            or task.get("visual_route") != "agent_adaptive"
            or store.get("deletion_receipt", task_id)):
        raise Conflict("Only an unpaused blocked adaptive observation can continue")
    nodes = [n for n in task["nodes"] if n["definition_id"] == "observe"
             and n["status"] == "failed" and n.get("error") == ERROR]
    if len(nodes) != 1:
        raise Conflict("A unique local observation boundary failure is required")
    node = nodes[0]
    if (node.get("external_pending") or node["attempt"] < 1
            or instructions_for(task, node) or task.get("confirmed_briefs", {}).get(node["asset_id"])):
        raise Conflict("Observation instructions or execution state require separate reconciliation")
    upstream = {}
    for name in ("prepare", "transcribe"):
        matches = [n for n in task["nodes"]
                   if n["definition_id"] == name and n["asset_id"] == node["asset_id"]]
        if len(matches) != 1 or matches[0]["status"] != "succeeded":
            raise Conflict("Original prepare and transcription results must remain successful")
        upstream[name] = matches[0]
    for other in task["nodes"]:
        if other["id"] in {node["id"], *(n["id"] for n in upstream.values())}:
            continue
        if other["status"] != "pending" or other["attempt"] != 0:
            raise Conflict("Another task branch has already started")
    checks = {
        "operation": {"running", "unknown", "pending"},
        "agent_trace": {"running", "unknown", "failed", "invalid_response"},
        "observation_decision": {"running", "unknown", "failed", "invalid_response"},
        "production_decision": {"running", "unknown", "pending", "invalid_response"},
        "imagegen_request": {"pending", "claimed", "running", "unknown"},
        "route_run": {"running", "pending", "unknown"},
        "route_probe": {"running", "pending", "unknown"},
        "question": {"pending", "open"},
        "conversation": {"queued", "running"},
    }
    related = {}
    for kind, statuses in checks.items():
        related[kind] = [r for r in store.list(kind) if r.get("task_id") == task_id]
        if any(r.get("status") in statuses for r in related[kind]):
            raise Conflict("Pending user or external work takes priority over observation continuation")
    prepared = upstream["prepare"]["output"]
    inputs = {
        "media": prepared["media"], "transcript": upstream["transcribe"]["output"],
        "frames": [{"time_ms": f["time_ms"]} for f in prepared["frames"]],
        "user_answer": node.get("output", {}).get("user_answer"),
    }
    root = (service.settings.runtime / "tasks" / task_id).resolve()
    files = {}
    asset = store.require("asset", node["asset_id"])
    _file(service, service.settings.runtime.resolve(), asset["path"], files, asset["sha256"])

    def frames(items):
        for frame in items:
            if (type(frame.get("time_ms")) is not int
                    or not 0 <= frame["time_ms"] < inputs["media"]["duration_ms"]):
                raise Conflict("Original observation frame time changed")
            _file(service, root, frame["path"], files, frame["sha256"])
        return [{"time_ms": f["time_ms"], "sha256": f["sha256"]} for f in items]

    initial = []
    for frame in prepared["frames"]:
        path = _file(service, root, frame["path"], files, frame.get("sha256"))
        initial.append({**frame, "sha256": files[str(path)]})
    frames(initial)
    session_id = observation_binding(task, node, inputs, asset["sha256"], initial)
    state = store.get("observation_session", session_id)
    if (not state or state.get("status") != "active" or state.get("turn") not in range(LIMIT - 1)
            or state.get("binding_sha256") != session_id or state.get("source_sha256") != asset["sha256"]
            or state.get("task_id") != task_id or state.get("node_id") != node["id"]
            or len(state.get("history", [])) != state["turn"]):
        raise Conflict("The original bounded observation session no longer matches")
    frames(state["frames"])
    matches = [r for r in related["observation_decision"]
               if r.get("session_id") == session_id and r.get("turn") == state["turn"]]
    if len(matches) != 1 or matches[0]["status"] != "succeeded":
        raise Conflict("A unique successful result awaiting local capture is required")
    decision = matches[0]
    intent = decision.get("intent", {})
    original_inputs = {k: intent.get("inputs", {}).get(k) for k in inputs}
    if (decision.get("node_id") != node["id"] or decision.get("source_sha256") != asset["sha256"]
            or original_inputs != inputs or frames(intent.get("frames", [])) != frames(state["frames"][-24:])):
        raise Conflict("Original observation result is bound to different inputs or images")
    result = ObservationDecision.model_validate(decision["result"]).model_dump()
    if result["action"] != "inspect" or inputs["media"]["duration_ms"] not in result["times_ms"]:
        raise Conflict("The successful result does not contain the original endpoint request")
    traces = [r for r in related["agent_trace"] if r.get("node_id") == node["id"]
              and r.get("decision_id") == decision["id"][:20] and r.get("attempt") == node["attempt"]]
    if len(traces) != 1 or traces[0]["status"] != "succeeded" or traces[0].get("result") != result:
        raise Conflict("Original successful observation trace is missing or ambiguous")
    directory = root / "nodes" / node["id"] / str(node["attempt"])
    prefix = decision["id"][:20]
    artifacts = [r for r in store.list("artifact") if r.get("task_id") == task_id
                 and r.get("node_id") == node["id"]]

    def artifact(name):
        path = directory / name
        matches = [r for r in artifacts if Path(r.get("path", "")) == path]
        if len(matches) != 1:
            raise Conflict("Original registered observation artifact is missing or ambiguous")
        return _json(service, root, path, files, matches[0]["sha256"])

    answer = artifact(prefix + "-agent-result.json")
    packet = artifact(prefix + "-work-packet.json")
    if ({k: v for k, v in answer.items() if k != "_media"} != result
            or packet.get("decision_id") != prefix or packet.get("inputs") != intent["inputs"]
            or frames(packet.get("images", [])) != frames(state["frames"][-24:])):
        raise Conflict("Original observation packet and result do not agree")
    recovery = directory / (prefix + "-codex-recovery")
    raw = {name: _json(service, root, recovery / (name + ".json"), files)
           for name in ("request", "receipt", "answer", "result", "event-summary", "event-result")}
    receipt, event, summary = raw["receipt"], raw["event-result"], raw["event-summary"]
    if (raw["result"] != result or raw["answer"] != result or event.get("answer") != result
            or event.get("answer_sha256") != _digest(result)
            or receipt.get("status") != "succeeded" or receipt.get("cli_exit_code") != 0
            or receipt.get("event_turn_state") != "completed" or receipt.get("event_stream_invalid") is not False
            or summary.get("event_turn_state") != "completed" or summary.get("event_stream_invalid") is not False
            or event.get("event_turn_state") != "completed" or event.get("result_source") != "completed_event"
            or not raw["request"].get("sha256")
            or any(r.get("request_sha256") != raw["request"]["sha256"] for r in (event, summary))):
        raise Conflict("Original CLI success evidence is incomplete or inconsistent")
    Draft202012Validator(raw["request"]["schema"]).validate(result)
    basis = _digest({
        "task": task, "asset": asset, "session": state, "decision": decision,
        "related": related, "files": files,
    })
    return {
        "expected_basis": basis, "task_id": task_id, "node_id": node["id"],
        "session_id": session_id, "decision_id": decision["id"], "turn": state["turn"],
        "current_attempt": node["attempt"], "next_attempt": node["attempt"] + 1,
        "source_sha256": asset["sha256"], "verified_files": len(files),
        "file_evidence_sha256": _digest(files), "paid_calls": task.get("paid_calls", 0),
        "remaining_decision_rounds": LIMIT - state["turn"] - 1,
        "scope": "Queue this node for Worker local capture and its remaining bounded observation",
        "replays_successful_inference": False, "resets_budget": False,
    }


def preview(service, task_id):
    with service.store.lock:
        try:
            return _context(service, service.store, task_id)
        except (OSError, KeyError) as exc:
            raise Conflict("Original observation evidence is unavailable; no continuation queued") from exc


def apply(service, task_id, expected_basis, *, approve=False):
    if not approve:
        raise Conflict("Explicit approval is required for observation continuation")
    receipt_id = _digest({"task_id": task_id, "expected_basis": expected_basis})
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        previous = store.get("source_observation_continuation", receipt_id)
        if previous:
            return previous
        try:
            fresh = _context(service, store, task_id)
        except (OSError, KeyError) as exc:
            raise Conflict("Original observation evidence is unavailable; no continuation queued") from exc
        if fresh["expected_basis"] != expected_basis:
            raise Conflict("Observation continuation basis changed; refresh the preview")
        task = store.require("task", task_id)
        node = next(n for n in task["nodes"] if n["id"] == fresh["node_id"])
        original = deepcopy(node)
        node.update(status="pending", error=None, finished_at=None)
        task.update(status="queued", error=None, updated_at=now())
        receipt = {
            "id": receipt_id, **fresh, "created_at": now(), "status": "queued",
            "original_node_sha256": _digest(original), "original_error": original["error"],
            "worker_scheduled": True, "model_calls_by_control": 0, "media_operations_by_control": 0,
        }
        store.put("source_observation_continuation", receipt)
        store.put("task", task)
        return receipt
