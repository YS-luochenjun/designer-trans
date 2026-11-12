"""A narrow local-tail continuation, serialized with normal Worker dispatch."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from ..application.source_observation_recovery import apply, preview


class ObservationContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_basis: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve: bool = False


def source_observation_routes(service, worker):
    router = APIRouter(prefix="/api/tasks")

    @router.get("/{task_id}/task-route")
    def task_route(task_id: str):
        from ..application.observation_route_handoff import route_status
        return route_status(service, task_id)

    @router.get("/{task_id}/observation-continuation")
    def context(task_id: str):
        with worker.guard(task_id):
            return preview(service, task_id)

    @router.post("/{task_id}/observation-continuation")
    def continue_observation(task_id: str, body: ObservationContinuation):
        with worker.guard(task_id):
            return apply(service, task_id, body.expected_basis, approve=body.approve)

    return router
