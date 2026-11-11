"""Resume only from a newly completed, retained route/work feedback event."""
from copy import deepcopy

from video_translation.application.agent_recovery import replaced_with_authorization
from video_translation.application.tasks import Conflict
from video_translation.execution.clean_progress import progress
from video_translation.execution.clean_contract import execution_contract
from video_translation.execution.clean_route_feedback import capture_completed_work, frontier
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status


def _preserved_downstream_unknowns(task, node):
    nodes = {n["id"]: n for n in task["nodes"]}
    ancestors, pending = set(), list(node["depends_on"])
    while pending:
        identifier = pending.pop()
        if identifier in ancestors:
            continue
        if identifier not in nodes:
            raise Conflict("Clean prerequisite is missing")
        ancestors.add(identifier)
        pending.extend(nodes[identifier]["depends_on"])
    if any(nodes[identifier]["status"] not in {"succeeded", "skipped"} for identifier in ancestors):
        raise Conflict("Clean prerequisites are not ready")
    descendants, pending = set(), [node["id"]]
    while pending:
        identifier = pending.pop()
        for child in task["nodes"]:
            if identifier in child["depends_on"] and child["id"] not in descendants:
                descendants.add(child["id"])
                pending.append(child["id"])
    unknowns = [n["id"] for n in task["nodes"] if n["status"] == "unknown"]
    if any(identifier not in descendants for identifier in unknowns):
        raise Conflict("Reconcile upstream or unrelated unknown results first")
    return unknowns


def resume_clean_route(service, task_id, expected_base_sha256):
    from video_translation.execution.adaptive import AdaptiveProduction
    from video_translation.execution.nodes import NodeExecutor

    task = service.store.require("task", task_id)
    sessions = [s for s in service.store.list("production_session") if s["task_id"] == task_id]
    if len(sessions) != 1:
        raise Conflict("Route resume requires one clean session")
    state = sessions[0]
    original_task, original_state = deepcopy(task), deepcopy(state)
    node = next(n for n in task["nodes"] if n["id"] == state["node_id"])
    if (task["status"] != "blocked" or task.get("control_paused")
            or node["definition_id"] != "clean" or node["status"] != "failed"
            or state.get("candidate") or state.get("pending_turn") is not None or state.get("finished")
            or any(n["status"] in {"running", "waiting_external"}
                   or n.get("external_pending") for n in task["nodes"])):
        raise Conflict("Route resume requires a stopped, unpaused task with no pending result")
    preserved_unknowns = _preserved_downstream_unknowns(task, node)
    if state["base"]["sha256"] != expected_base_sha256:
        raise Conflict("Base changed; inspect current state")
    decisions = service.store.list("production_decision")
    operations = service.store.list("operation")
    if any(r.get("task_id") == task_id and r["status"] == "running"
           for r in [*decisions, *operations]):
        raise Conflict("An original task operation is still running")
    if any(d.get("node_id") == node["id"] and (
               d.get("key") == f"clean-{state['turn']}" and d["status"] == "succeeded"
               or d["status"] in {"running", "unknown", "invalid_response"}
               and not replaced_with_authorization(service.store, d, "production_decision"))
           for d in decisions):
        raise Conflict("Reconcile the current work result first")
    if any(op.get("node_id") == node["id"] and op["status"] in {"running", "unknown"}
           and not replaced_with_authorization(service.store, op, "operation")
           for op in operations):
        raise Conflict("Reconcile the original clean operation first")
    if any(r.get("task_id") == task_id and r["status"] in {"pending", "claimed", "unknown"}
           for r in service.store.list("imagegen_request")):
        raise Conflict("Reconcile the original image tool request first")
    controller = AdaptiveProduction(NodeExecutor(service), task, node)
    controller.verified(state["base"])
    for source_id, override in state.get("text_overrides", {}).items():
        controller.texts[source_id].update(override)
    queue = progress(controller.texts, state)
    if not queue["all_deferred"] or not capture_completed_work(state, queue):
        raise Conflict("No newly completed route/work outcome; do not blindly retry")
    methods = next((r for r in task["resource_snapshot"].get("knowledge", [])
                    if r["id"] == "video-translation.methods" and r["status"] == "active"), None)
    available = frontier(controller, state, queue, execution_contract()["revision"],
                         methods["sha256"]) if methods else []
    if not available:
        raise Conflict("No unassigned local checks remain")
    # Retain the source/interval obstacles; the real router decides what to inspect.
    receipt = {"at": now(), "base_sha256": expected_base_sha256,
               "completed_route_review": state["route_work_order"]["route_review"],
               "unassigned_local_checks": available, "recipe_supplied": False,
               "generation_authorized": False,
               "preserved_unknown_nodes": preserved_unknowns}
    state.setdefault("route_resume_events", []).append(receipt)
    state["progress"] = queue
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if (tx.require("task", task_id) != original_task
                or tx.require("production_session", state["id"]) != original_state):
            raise Conflict("Task changed; no route resume was saved")
        tx.put("task", task)
        tx.put("production_session", state)
    return {"task_id": task_id, "status": task["status"], "route_resume": receipt,
            "accepted_unchanged": True, "deferrals_unchanged": True}
