"""Explicit visual recovery controls; model calls remain in the normal Worker."""
from fastapi import APIRouter, Query

from ..application.visual_recovery import authorize, context
from ..domain.visual_recovery import VisualRecoveryAuthorization


def visual_recovery_routes(service, worker, manager):
    router = APIRouter(prefix="/api/tasks")

    @router.get("/{task_id}/visual-recovery-context")
    def preview(task_id: str, expected_decision_id: str = Query(pattern=r"^[0-9a-f]{64}$")):
        with worker.guard(task_id):
            return context(service, task_id, expected_decision_id)

    @router.post("/{task_id}/authorize-visual-agent-replacement")
    def approve(task_id: str, body: VisualRecoveryAuthorization):
        with worker.guard(task_id):
            result = authorize(service, task_id, body)
        manager.wake.set()
        return result

    return router
