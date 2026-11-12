"""Change a stopped task's output objective without scheduling production."""
from copy import deepcopy

from video_translation.domain.execution import identity
from video_translation.execution.convergence import ERROR
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status
from .tasks import Conflict


def context(service, task_id, *, store=None):
    store = store or service.store
    task = store.require("task", task_id)
    sessions = [s for s in store.list("production_session") if s.get("task_id") == task_id]
    unsettled = sorted((kind, r["id"], identity(r))
                       for kind in ("operation", "production_decision", "imagegen_request")
                       for r in store.list(kind)
                       if r.get("task_id") == task_id
                       and r.get("status") in {"running", "unknown", "invalid_response", "pending", "claimed"})
    return {"task_id": task_id, "output_goal": task.get("output_goal", "quality_approved"),
            "basis_sha256": identity({"task": task, "sessions": sorted(sessions, key=lambda s: s["id"]),
                                     "unsettled": unsettled}),
            "control_paused": bool(task.get("control_paused")), "scheduled": False}


def change(service, task_id, goal, expected_basis):
    if goal not in {"complete_candidate", "quality_approved"}:
        raise ValueError("Unknown output goal")
    task = service.store.require("task", task_id)
    if context(service, task_id)["basis_sha256"] != expected_basis:
        raise Conflict("Task or production evidence changed; reload output goal context")
    if (not task.get("control_paused") or task["status"] in {"cancelled", "completed"}
            or service.store.get("deletion_receipt", task_id)
            or any(n["status"] in {"running", "unknown", "waiting_external"}
                   or n.get("external_pending") for n in task["nodes"])):
        raise Conflict("Output goal can change only on a paused, settled task")
    if task.get("visual_route") != "agent_adaptive":
        raise Conflict("Complete-candidate output requires adaptive production")
    from .agent_recovery import replaced_with_authorization
    for kind in ("operation", "production_decision", "imagegen_request"):
        if any(row.get("task_id") == task_id
               and row.get("status") in {"running", "unknown", "invalid_response", "pending", "claimed"}
               and not replaced_with_authorization(service.store, row, kind)
               for row in service.store.list(kind)):
            raise Conflict("Reconcile original unsettled requests before changing output goal")
    if any(n.get("output") and n["definition_id"] in {"typography", "visual", "compose", "review", "deliver"}
           for n in task["nodes"]):
        raise Conflict("Existing downstream outputs require directed rework, not a goal-only change")
    original = deepcopy(task)
    if task.get("output_goal", "quality_approved") == goal:
        return {**context(service, task_id), "already_set": True}
    task.setdefault("output_goal_history", []).append({
        "at": now(), "previous": task.get("output_goal", "quality_approved"), "goal": goal,
        "reason": "Explicit task output objective; no budget, quality or production authorization changed",
    })
    task["output_goal"] = goal
    for node in task["nodes"]:
        if (goal == "complete_candidate" and node["definition_id"] == "clean"
                and node["status"] == "failed" and (
                    node.get("error") == ERROR
                    or "clean turn allowance exhausted" in (node.get("error") or "")
                    or "已停止重复观察" in (node.get("error") or ""))):
            node.setdefault("history", []).append({
                k: deepcopy(node.get(k)) for k in ("attempt", "status", "output", "error", "finished_at")})
            node.update(status="pending", error=None, finished_at=None)
    task.update(updated_at=now(), status=task_status(task))
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if (tx.require("task", task_id) != original
                or context(service, task_id, store=tx)["basis_sha256"] != expected_basis):
            raise Conflict("Task changed during output goal update")
        tx.put("task", task)
    return {**context(service, task_id), "quality_accepted": False,
            "pause_preserved": True, "scheduled": False}
