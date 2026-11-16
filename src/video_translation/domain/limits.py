"""Nullable external limits: None is unlimited; zero disables new usage."""


def testing_unlimited(task: dict) -> bool:
    return task.get("execution_snapshot", {}).get("budget_mode") == "testing_unlimited"


def effective_limit(task: dict, field: str, default=0):
    if testing_unlimited(task) or field in {"max_visual_steps", "max_revisions"}:
        return None
    return task.get(field, default)


def allows_revision(task: dict, used: int) -> bool:
    limit = effective_limit(task, "max_revisions")
    return limit is None or used < limit


def budget_policy(task: dict) -> dict:
    unlimited = testing_unlimited(task)
    return {
        "mode": "testing_unlimited" if unlimited else "bounded",
        "usage_quotas_enforced": not unlimited,
        "action_quotas_enforced": False,
        "workspace_quotas_enforced": False,
        "storage_admission": "actual_free_disk",
        "review_thresholds": {
            "max_visual_steps": task.get("max_visual_steps"),
            "max_revisions": task.get("max_revisions"),
            **{key: value for key, value in (task.get("execution_snapshot", {}).get(
                "resource_budget") or {}).items()
               if key in {"max_clean_turns", "max_external_calls", "max_recovery_calls"}},
        },
        "accounting_retained": True,
        "instruction": (
            "Testing has no call, credit, cleanup-action or revision quota. Historical numeric "
            "allowances and old assignment action counts are reference values, not stop reasons. "
            "Continue useful evidence-led work; review stagnation and change methods. "
            "Workspace byte/file totals are reporting only, never stop reasons. Check actual free "
            "disk for the next output. Original request identity, unresolved results and quality gates still apply."
            if unlimited else
            "Action, cleanup-turn, observation and rework counts are review thresholds, not stop "
            "conditions. Route reviews progress, changes methods and dispatches useful remaining work. "
            "Explicit task spending/call allowances still apply. Retain cumulative usage and unknown "
            "costs, original request identities, user pause and quality gates. Workspace byte/file "
            "totals are reporting only; check actual free disk for the next output."
        ),
    }


def allows_usage(task: dict, field: str, total: int | float) -> bool:
    limit = effective_limit(task, field)
    return limit is None or total <= limit


def allows_new_call(task: dict, field: str, used: int = 0) -> bool:
    return allows_usage(task, field, used + 1)
