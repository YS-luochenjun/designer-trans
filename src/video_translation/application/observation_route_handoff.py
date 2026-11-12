"""Worker-owned, one-time handoff of legacy observation questions to task Route."""
from copy import deepcopy
from pathlib import Path

from ..execution.adaptive_observe import _digest, music_fact, retained_inventory
from ..persistence.store import now
from ..persistence.transaction import TransactionStore
from .tasks import Conflict


def _context(service, store, task_id):
    task = store.require("task", task_id)
    if (task["status"] != "waiting_user" or task.get("control_paused")
            or task.get("visual_route") != "agent_adaptive"
            or store.get("deletion_receipt", task_id)):
        raise Conflict("Only an active adaptive observation question can be handed to Route")
    questions = [q for q in store.list("question")
                 if q.get("task_id") == task_id and q.get("status") == "open"]
    if len(questions) != 1:
        raise Conflict("A unique untouched observation question is required")
    question = questions[0]
    nodes = [n for n in task["nodes"] if n["id"] == question["node_id"]]
    if len(nodes) != 1:
        raise Conflict("Observation question owner is missing")
    node = nodes[0]
    if (node["definition_id"] != "observe" or node["status"] != "waiting_user"
            or node["attempt"] != question["attempt"] or question["kind"] != "clarification"
            or question.get("answer") is not None or question.get("context")
            or node.get("external_pending") or node.get("error")):
        raise Conflict("User instructions and classified Route questions must stay with their owner")
    output = node["output"]
    session_id = output.get("_media", {}).get("session_id")
    state = store.get("observation_session", session_id) if session_id else None
    if (not state or state.get("task_id") != task_id or state.get("node_id") != node["id"]
            or state.get("status") != "finished" or state.get("terminal_action") != "clarify"
            or state.get("inventory", {}).get("unknowns") != output.get("unknowns")
            or len(state.get("history", [])) != state.get("turn")
            or not state.get("history")):
        raise Conflict("The original completed clarify session must be retained")
    if any(output.get(key) != value for key, value in state["inventory"].items()):
        raise Conflict("The node inventory changed after its original decision")
    decisions = [store.require("observation_decision", h["decision_id"]) for h in state["history"]]
    if any(d.get("status") != "succeeded" or d.get("session_id") != session_id for d in decisions):
        raise Conflict("Unsettled original decisions cannot be replaced")
    terminal = decisions[-1].get("result", {}).get("inventory")
    if terminal != state["inventory"]:
        inputs = decisions[-1].get("intent", {}).get("inputs", {})
        if (not terminal or not inputs.get("media") or
                retained_inventory(terminal, music_fact(inputs)) != state["inventory"]):
            raise Conflict("The original terminal decision changed")
    for other in task["nodes"]:
        if other["id"] == node["id"]:
            continue
        if other["definition_id"] in {"prepare", "transcribe"} and other["asset_id"] == node["asset_id"]:
            if other["status"] not in {"succeeded", "skipped"}:
                raise Conflict("Original upstream results must stay available")
        elif other["status"] != "pending" or other["attempt"] != 0:
            raise Conflict("Another task branch already needs separate coordination")
    pending = {"operation": {"running", "unknown", "pending"},
               "agent_trace": {"running", "unknown", "failed"},
               "imagegen_request": {"pending", "claimed", "running", "unknown"},
               "observation_decision": {"running", "unknown", "failed"},
               "task_route_decision": {"running", "unknown", "failed"},
               "conversation": {"queued", "running"}}
    for kind, statuses in pending.items():
        if any(row.get("task_id") == task_id and row.get("status") in statuses
               for row in store.list(kind)):
            raise Conflict("Original requests or user messages take priority")
    asset = store.require("asset", node["asset_id"])
    if (asset.get("sha256") != state["source_sha256"]
            or service.hash_file(service.checked_path(asset)) != state["source_sha256"]):
        raise Conflict("Observation source changed")
    root = (service.settings.runtime / "tasks" / task_id).resolve()
    for frame in state["frames"]:
        path = Path(frame["path"])
        if (path.is_symlink() or not path.resolve().is_relative_to(root)
                or service.hash_file(path) != frame["sha256"]):
            raise Conflict("Observation evidence changed")
    return task, node, question, state


def queue_legacy_observation(service, task_id):
    """No inference/media here: only Worker can execute the newly queued handoff."""
    with service.store.connection() as db:
        store = TransactionStore(service.store, db)
        task, node, question, state = _context(service, store, task_id)
        identity = _digest({"task_id": task_id, "question_id": question["id"], "session_id": state["id"]})
        if store.get("observation_route_handoff", identity):
            return False
        original = deepcopy(question)
        question.update(status="superseded", superseded_by="task_route", superseded_at=now())
        node.update(status="pending", error=None, finished_at=None)
        task.update(status="queued", updated_at=now())
        store.put("observation_route_handoff", {
            "id": identity, "task_id": task_id, "node_id": node["id"], "session_id": state["id"],
            "original_question": original, "original_inventory_sha256": _digest(state["inventory"]),
            "status": "queued", "created_at": now(),
            "model_calls_by_control": 0, "media_operations_by_control": 0,
        })
        store.put("question", question)
        store.put("task", task)
    return True


def route_status(service, task_id):
    service.store.require("task", task_id)
    rows = [row for row in service.store.list("task_route_session") if row["task_id"] == task_id]
    rows.sort(key=lambda row: (row["created_at"], row["id"]))
    return {"task_id": task_id, "protocol_version": "1", "scope": "source_observation",
            "sessions": [{k: row.get(k) for k in (
                "id", "node_id", "status", "route_turn", "work_turn", "reason", "issues",
                "observation_session_id", "last_decision_id")} for row in rows]}


def route_owned_nodes(store, task_id):
    return {row["node_id"] for row in store.list("task_route_session")
            if row["task_id"] == task_id
            and row["status"] in {"active", "blocked", "failed", "unknown", "waiting_user"}}
