"""Finite task resources; limits never approve media or replay old operations."""
from video_translation.application.tasks import Conflict
from video_translation.domain.execution import ExecutionProfile

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
    # Frozen resource counts are planning references; task spending guards own admission.
    return


def guard_recovery_call(store, task):
    return


def guard_clean_turn(task, state):
    execution = state.get("new_execution") or {}
    budget = budget_for(task)
    thresholds = [value for value in (
        execution.get("new_clean_limit"), budget.max_clean_turns if budget else None)
        if value is not None]
    interval = max(1, min(thresholds)) if thresholds else None
    last = state.get("last_count_review_turn", 0)
    if interval and state["turn"] >= last + interval:
        state["completion_review_due"] = True
    else:
        state.pop("completion_review_due", None)
