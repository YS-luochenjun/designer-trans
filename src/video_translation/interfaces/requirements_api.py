"""Requirement-facing routes. No advertising operations or remote write endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field
from uuid import UUID

from video_translation.application.requirements import RequirementService
from video_translation.application.intake import IntakeCreate, IntakePreview, IntakeService


class SyncFireplayRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    delivery_types: list[str] = Field(min_length=1, max_length=200)


class AcceptRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: UUID
    task_id: str


def requirement_routes(tasks, fireplay, intake=None):
    router = APIRouter(prefix="/api")
    requirements = RequirementService(tasks)
    intake = intake or IntakeService(tasks, fireplay)
    from video_translation.integrations.fireplay.login import FireplayLogin
    login = FireplayLogin(tasks.settings.root, fireplay)

    @router.post("/fireplay/login")
    def start_login(request: Request):
        # Opening a local browser must be a same-origin action on the local workbench.
        origin = request.headers.get("origin", "")
        if (request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or (origin and urlsplit(origin).netloc != request.url.netloc)
                or request.headers.get("sec-fetch-site") == "cross-site"):
            raise HTTPException(403, "请在本机工作台中发起登录")
        return login.start()

    @router.get("/fireplay/login")
    def login_status():
        return login.status()

    @router.post("/fireplay/intake/preview")
    def preview_intake(body: IntakePreview):
        return intake.preview(body.reference)

    @router.post("/fireplay/intake/create", status_code=201)
    def create_intake(body: IntakeCreate):
        return intake.create(body)

    @router.get("/fireplay/delivery-options")
    def delivery_options():
        return fireplay.delivery_options()

    @router.post("/fireplay/sync")
    def sync_requirements(body: SyncFireplayRequirements | None = None):
        return fireplay.sync(None if body is None else body.delivery_types)

    @router.get("/fireplay/connection")
    def connection():
        return fireplay.connection()

    @router.post("/fireplay/probe")
    def probe():
        return fireplay.connection(probe=True)

    @router.get("/fireplay/requirements/{requirement_id}/brief")
    def brief(requirement_id: UUID):
        return fireplay.brief(str(requirement_id))

    @router.get("/requirements")
    def list_requirements():
        records = requirements.list()
        return {"items": records, "total": len(records)}

    @router.post("/requirements/accept")
    def accept(body: AcceptRequirement):
        # Refresh from the authenticated source, not arbitrary caller-supplied JSON.
        brief = fireplay.brief(str(body.requirement_id))
        # Keep asset validation/hash and reference writes atomic against deletion.
        # Remote brief I/O stays outside the local store lock.
        with tasks.store.lock:
            task = tasks.store.require("task", body.task_id)
            record = requirements.register(brief, task["asset_ids"], task["target_locales"])
            requirements.accept(record["id"])
            return requirements.link_task(record["id"], task["id"])

    @router.get("/requirements/{requirement_id}/submissions")
    def submissions(requirement_id: UUID):
        records = requirements.submissions(str(requirement_id))
        return {"items": records, "total": len(records)}

    @router.post("/tasks/{task_id}/submission-preview")
    def submission_preview(task_id: str):
        preview = requirements.prepare_submission(task_id)
        # Safe local download links help manual handoff while the live write contract is pending.
        return {**preview, "downloads": [{"locale": item["locale"], "sha256": item["sha256"],
                "url": f"/api/artifacts/{item['artifact_id']}/content?download=true"}
                for item in preview["deliverables"]]}

    return router
