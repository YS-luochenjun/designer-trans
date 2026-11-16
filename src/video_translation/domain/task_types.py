"""Execution ownership is determined by the business kind, never a client flag."""


def execution_owner(task):
    return "facefusion" if task.get("task_kind") == "face_swap" else "route_worker"


def uses_route_work(task):
    return execution_owner(task) == "route_worker"
