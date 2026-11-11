"""Nullable external limits: None is unlimited; zero disables new usage."""


def allows_usage(task: dict, field: str, total: int | float) -> bool:
    limit = task.get(field, 0)
    return limit is None or total <= limit


def allows_new_call(task: dict, field: str, used: int = 0) -> bool:
    return allows_usage(task, field, used + 1)
