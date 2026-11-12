"""Finite task resources; limits never approve media or replay old operations."""
from video_translation.application.tasks import Conflict
from video_translation.domain.execution import ExecutionProfile
from video_translation.domain.limits import testing_unlimited
from video_translation.integrations.agents.errors import CapabilityUnavailable

from .media import MediaError


def budget_for(task):
    return ExecutionProfile.model_validate(task.get("execution_snapshot", {})).resource_budget


def guard_storage(service, task):
    if budget_for(task) is None:
        return
    from .workspace import workspace_for
    workspace = workspace_for(service, task)
    try:
        workspace.preflight(workspace.root / "resource-preflight.json", 0)
    except MediaError as exc:
        raise Conflict(f"Task resource storage block: {exc}") from exc


def guard_external_call(task):
    if testing_unlimited(task):
        return
    budget = budget_for(task)
    if budget and task.get("paid_calls", 0) >= budget.max_external_calls:
        raise Conflict("Task resource external call allowance exhausted; no new dispatch")


def guard_recovery_call(store, task):
    if testing_unlimited(task):
        return
    budget = budget_for(task)
    if budget is None:
        return
    count = sum(row.get("task_id") == task["id"]
                for kind in ("route_run", "route_probe") for row in store.list(kind))
    count += sum(row.get("task_id") == task["id"] and bool(row.get("started_at"))
                 for row in store.list("task_route_event"))
    if count >= budget.max_recovery_calls:
        raise Conflict("Task resource recovery call allowance exhausted; no new diagnostic")


def guard_clean_turn(task, state):
    if testing_unlimited(task):
        state.pop("completion_review_due", None)
        return
    execution = state.get("new_execution") or {}
    if execution.get("new_clean_limit") is not None and state["turn"] >= execution["new_clean_limit"]:
        raise CapabilityUnavailable(
            "Explicit cleanup execution action window exhausted; original progress and costs retained")
    budget = budget_for(task)
    if not budget or state["turn"] < budget.max_clean_turns:
        state.pop("completion_review_due", None)
        return
    if budget and state["turn"] >= budget.max_clean_turns:
        from .review_draft import complete_candidate
        if complete_candidate(task):
            state["completion_review_due"] = True
            return
        raise CapabilityUnavailable(
            "Task resource clean turn allowance exhausted; unresolved coverage and candidates retained")
