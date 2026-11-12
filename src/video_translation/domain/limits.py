"""Nullable external limits: None is unlimited; zero disables new usage."""


def testing_unlimited(task: dict) -> bool:
    return task.get("execution_snapshot", {}).get("budget_mode") == "testing_unlimited"


def effective_limit(task: dict, field: str, default=0):
    return None if testing_unlimited(task) else task.get(field, default)


def allows_revision(task: dict, used: int) -> bool:
    limit = effective_limit(task, "max_revisions")
    return limit is None or used < limit


def budget_policy(task: dict) -> dict:
    unlimited = testing_unlimited(task)
    return {
        "mode": "testing_unlimited" if unlimited else "bounded",
        "usage_quotas_enforced": not unlimited,
        "accounting_retained": True,
        "instruction": (
            "Testing has no call, credit, cleanup-action or revision quota. Historical numeric "
            "allowances and old assignment action counts are reference values, not stop reasons. "
            "Continue useful evidence-led work; review stagnation and change methods. "
            "Original request identity, unresolved results, storage protection and quality gates still apply."
            if unlimited else "Use the task's existing numeric allowances and retain cumulative usage."
        ),
    }


def allows_usage(task: dict, field: str, total: int | float) -> bool:
    limit = effective_limit(task, field)
    return limit is None or total <= limit


def allows_new_call(task: dict, field: str, used: int = 0) -> bool:
    return allows_usage(task, field, used + 1)
