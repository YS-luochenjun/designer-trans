"""Explicit standing delegation; automatic recovery stays in the project supervisor."""
from fastapi import APIRouter

from ..application.route_recovery import configure
from ..domain.route_recovery import RouteAutonomy


def route_recovery_routes(service, worker, manager):
    router = APIRouter(prefix="/api/tasks")

    @router.put("/{task_id}/route-autonomy")
    def delegate(task_id: str, body: RouteAutonomy):
        with worker.guard(task_id):
            policy = configure(service, task_id, body)
        manager.wake.set()
        return policy

    @router.get("/{task_id}/route-autonomy")
    def snapshot(task_id: str):
        from ..application.reconciliation import snapshot as reconciliation_snapshot
        task = service.store.require("task", task_id)
        return {
            "policy": service.store.get("route_policy", task_id),
            "status": service.store.get("route_status", task_id),
            "reconciliation": reconciliation_snapshot(service, task),
            "runs": sorted([r for r in service.store.list("route_run") if r["task_id"] == task_id],
                           key=lambda r: (r.get("created_at", ""), r["id"]), reverse=True)[:30],
            "contract_diagnostics": [p for p in service.store.list("route_probe")
                                     if p["task_id"] == task_id][:30],
        }

    return router
