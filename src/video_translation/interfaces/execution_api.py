"""Read-only execution health and directed-rework planning queries."""
from fastapi import APIRouter

from video_translation.application.execution_diagnostics import diagnostics, rework_plan


def execution_routes(service):
    router = APIRouter(prefix="/api/tasks")

    @router.get("/{task_id}/execution-diagnostics")
    def execution_diagnostics(task_id: str, include_storage: bool = False):
        return diagnostics(service, task_id, include_storage=include_storage)

    @router.get("/{task_id}/execution-rework-plan")
    def execution_rework(task_id: str, session_id: str, incident_id: str, expected_base_sha256: str):
        return rework_plan(service, task_id, session_id, incident_id, expected_base_sha256)

    return router
