"""Bounded work-agent reassessment after an explicit local-method upgrade."""
from copy import deepcopy

from video_translation.agent_runtime.resources import ResourceCatalog
from video_translation.execution.clean_contract import REVISION
from video_translation.execution.clean_progress import fingerprint, progress
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status
from .tasks import Conflict

METHODS = "video-translation.methods"


def reassess_clean(service, task_id, source_ids, expected_base_sha256, reason):
    """Caller holds worker guard. Does not supply a recipe or accept any pixels."""
    from video_translation.execution.adaptive import AdaptiveProduction
    from video_translation.execution.nodes import NodeExecutor

    if not source_ids or len(source_ids) != len(set(source_ids)):
        raise ValueError("Provide unique source IDs")
    task = service.store.require("task", task_id)
    original_task = deepcopy(task)
    if task["status"] != "blocked" or task.get("control_paused"):
        raise Conflict("Reassessment requires a blocked, unpaused task")
    if any(n["status"] in {"running", "unknown", "waiting_external"} or n.get("external_pending")
           for n in task["nodes"]):
        raise Conflict("Reconcile active or unknown node results first")
    sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
    if len(sessions) != 1:
        raise Conflict("Reassessment requires one clean session")
    state = sessions[0]
    original_state = deepcopy(state)
    node = next(n for n in task["nodes"] if n["id"] == state["node_id"])
    if node["definition_id"] != "clean" or node["status"] != "failed":
        raise Conflict("Reassessment requires a stopped clean node")
    if state.get("candidate") or state.get("pending_turn") is not None:
        raise Conflict("Review the existing candidate/action first")
    if state["base"]["sha256"] != expected_base_sha256:
        raise Conflict("Base changed; inspect current evidence")
    if any(d.get("node_id") == node["id"] and d.get("key") == f"clean-{state['turn']}"
           and d.get("status") in {"running", "unknown", "succeeded"}
           for d in service.store.list("production_decision")):
        raise Conflict("Reconcile the current Agent decision first")
    controller = AdaptiveProduction(NodeExecutor(service), task, node)
    controller.verified(state["base"])
    for source_id, override in state.get("text_overrides", {}).items():
        controller.texts[source_id].update(override)
    _, clock = controller.media._info(controller.source)
    state["coverage_clock"] = {k: clock[k] for k in ("rate", "frames")}
    summary = progress(controller.texts, state)
    remaining = {r["source_id"]: r["intervals_ms"] for r in summary["remaining"]}
    current = [r for r in ResourceCatalog(service.settings.root).list("knowledge")
               if r["id"] == METHODS and r["status"] == "active"
               and (not r["nodes"] or "clean" in r["nodes"])]
    if len(current) != 1:
        raise Conflict("Local-method knowledge is unavailable or ambiguous")
    methods = current[0]
    markers = state.setdefault("method_reassessments", {})
    keys = {}
    for source_id in source_ids:
        if source_id not in remaining or source_id not in state.get("deferred", {}):
            raise Conflict("Select an unresolved, deferred source")
        keys[source_id] = fingerprint({
            "source_id": source_id, "intervals": remaining[source_id],
            "scope": controller.texts[source_id], "contract": REVISION,
            "methods_sha256": methods["sha256"],
        })
        if keys[source_id] in markers:
            raise Conflict("This source already reassessed the same method version; new evidence is required")
    previous = [r for r in task["resource_snapshot"]["knowledge"] if r["id"] == METHODS]
    task["resource_snapshot"]["knowledge"] = [
        r for r in task["resource_snapshot"]["knowledge"] if r["id"] != METHODS] + [methods]
    event = {
        "at": now(), "source_ids": source_ids, "reason": reason,
        "contract_revision": REVISION, "methods_version": methods["version"],
        "methods_sha256": methods["sha256"], "base_sha256": expected_base_sha256,
    }
    task.setdefault("resource_upgrades", []).append({
        **event, "previous_resources": previous, "resource_ids": [METHODS]})
    for source_id, key in keys.items():
        obstacle = state["deferred"].pop(source_id)
        state.setdefault("deferred_history", []).append({
            "source_id": source_id, "obstacle": obstacle,
            "reason": "Explicit versioned method reassessment; no repair or acceptance supplied",
            "reassessment_key": key, "turn": state["turn"],
        })
        markers[key] = {**event, "source_id": source_id}
    state["method_reassessment"] = event
    first = remaining[source_ids[0]][0]
    state["times_ms"] = sorted({first[0], (first[0] + first[1]) // 2, first[1] - 1})
    state["progress"] = progress(controller.texts, state)
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as connection:
        transaction = TransactionStore(service.store, connection)
        if (transaction.require("task", task_id) != original_task
                or transaction.require("production_session", state["id"]) != original_state):
            raise Conflict("Task or session changed; reassessment was not applied")
        transaction.put("task", task)
        transaction.put("production_session", state)
    return {"task_id": task_id, "status": task["status"], "reassessment": event,
            "accepted_unchanged": True, "recipe_supplied": False}
