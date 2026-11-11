from fastapi import APIRouter
from pydantic import Field

from video_translation.domain.models import Contract
from video_translation.application.clean_reassessment import reassess_clean
from video_translation.application.clean_acceptance_audit import audit_clean_acceptance
from video_translation.application.clean_video_trial import enable_video_trial
from video_translation.application.clean_review_reassessment import reassess_clean_review
from video_translation.application.clean_route_resume import resume_clean_route
from video_translation.application.review_draft import enable_review_draft
from video_translation.application.clean_resume import resume_clean
from video_translation.application.completion import enable_completion, restore_stage_gates


class ReassessmentRequest(Contract):
    source_ids: list[str] = Field(min_length=1, max_length=12)
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=2400)


class AcceptanceAuditRequest(Contract):
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VideoTrialRequest(Contract):
    source_id: str = Field(min_length=1, max_length=200)
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewReassessmentRequest(Contract):
    candidate_id: str = Field(min_length=1, max_length=200)
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RouteResumeRequest(Contract):
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentReplacementRequest(RouteResumeRequest):
    expected_decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_answer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MissingAgentReplacementRequest(RouteResumeRequest):
    expected_decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImagegenReceipt(Contract):
    claim_token: str = Field(min_length=48, max_length=48)
    generated_image_path: str | None = Field(default=None, min_length=1, max_length=2000)
    error: str | None = Field(default=None, min_length=1, max_length=1600)


def clean_resume_routes(service, worker):
    router = APIRouter(prefix="/api")

    @router.post("/tasks/{task_id}/authorize-agent-replacement")
    def agent_replacement(task_id: str, body: AgentReplacementRequest):
        from video_translation.application.agent_replacement import authorize_clean_agent_replacement
        with worker.guard(task_id):
            return authorize_clean_agent_replacement(service, task_id, **body.model_dump())

    @router.post("/tasks/{task_id}/authorize-missing-agent-replacement")
    def missing_agent_replacement(task_id: str, body: MissingAgentReplacementRequest):
        from video_translation.application.agent_replacement import authorize_missing_clean_agent_replacement
        with worker.guard(task_id):
            return authorize_missing_clean_agent_replacement(service, task_id, **body.model_dump())

    @router.get("/tasks/{task_id}/imagegen-requests")
    def imagegen_requests(task_id: str):
        from video_translation.application.imagegen_bridge import public_requests
        service.store.require("task", task_id)
        return public_requests(service, task_id)

    @router.post("/tasks/{task_id}/imagegen-requests/{request_id}/claim")
    def imagegen_claim(task_id: str, request_id: str):
        from video_translation.application.imagegen_bridge import claim
        with worker.guard(task_id):
            return claim(service, task_id, request_id)

    @router.post("/tasks/{task_id}/imagegen-requests/{request_id}/complete")
    def imagegen_complete(task_id: str, request_id: str, body: ImagegenReceipt):
        from video_translation.application.imagegen_bridge import complete
        with worker.guard(task_id):
            return complete(service, task_id, request_id, **body.model_dump())

    @router.post("/tasks/{task_id}/enable-review-draft")
    def review_draft(task_id: str, body: RouteResumeRequest):
        with worker.guard(task_id):
            return enable_review_draft(service, task_id, **body.model_dump())

    @router.post("/tasks/{task_id}/resume-clean-route")
    def resume_route(task_id: str, body: RouteResumeRequest):
        with worker.guard(task_id):
            return resume_clean_route(service, task_id, **body.model_dump())

    @router.post("/tasks/{task_id}/reassess-clean-review")
    def review_reassessment(task_id: str, body: ReviewReassessmentRequest):
        with worker.guard(task_id):
            return reassess_clean_review(service, task_id, **body.model_dump())

    @router.post("/tasks/{task_id}/clean-video-trial")
    def video_trial(task_id: str, body: VideoTrialRequest):
        with worker.guard(task_id):
            return enable_video_trial(service, task_id, **body.model_dump())

    @router.post("/tasks/{task_id}/audit-clean-acceptance")
    def audit(task_id: str, body: AcceptanceAuditRequest):
        with worker.guard(task_id):
            return audit_clean_acceptance(service, task_id, **body.model_dump())

    @router.post("/tasks/{task_id}/reassess-clean")
    def reassess(task_id: str, body: ReassessmentRequest):
        with worker.guard(task_id):
            return reassess_clean(service, task_id, **body.model_dump())

    @router.post("/tasks/{task_id}/resume-clean")
    def resume(task_id: str):
        with worker.guard(task_id):
            return resume_clean(service, task_id)

    @router.post("/tasks/{task_id}/completion-first")
    def complete(task_id: str):
        with worker.guard(task_id):
            return enable_completion(service, task_id)

    @router.post("/tasks/{task_id}/restore-stage-gates")
    def restore(task_id: str):
        with worker.guard(task_id):
            return restore_stage_gates(service, task_id)

    return router
