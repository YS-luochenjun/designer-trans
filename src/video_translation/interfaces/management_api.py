"""Local project supervisor endpoints."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from ..application.architecture import architecture
from ..application.intake import IntakeCreate
from ..domain.management import ManagerAnswer, ManagerMessage, ManagerSettings
from ..domain.models import Answer
from ..persistence.store import identifier


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approve: bool


def management_routes(manager, worker):
    router = APIRouter(prefix="/api/manager")

    @router.get("")
    def state():
        return manager.snapshot()

    @router.get("/architecture")
    def project_architecture():
        return architecture()

    @router.put("/settings")
    def settings(body: ManagerSettings):
        return manager.configure(body)

    @router.post("/messages", status_code=202)
    def message(body: ManagerMessage):
        return manager.message(body)

    @router.post("/patrol", status_code=202)
    def patrol():
        return manager.enqueue("patrol", {}, identifier())

    @router.post("/requirements-check", status_code=202)
    def requirements_check():
        return manager.enqueue("requirements_check", {}, identifier())

    @router.post("/actions/{action_id}/authorize")
    def authorize(action_id: str, body: Authorization):
        return manager.authorize(action_id, body.approve)

    @router.post("/incidents/{incident_id}/dismiss")
    def dismiss(incident_id: str):
        return manager.dismiss(incident_id)

    @router.post("/questions/{question_id}/answer")
    def answer(question_id: str, body: ManagerAnswer):
        return manager.answer(question_id, body.answer)

    @router.post("/tasks/{task_id}/questions/{question_id}/answer")
    def production_answer(task_id: str, question_id: str, body: Answer):
        return manager.answer_production(task_id, question_id, body.answer, body.feedback)

    @router.post("/intakes", status_code=202)
    def intake(body: IntakeCreate):
        return manager.prepare_intake(body)

    return router
