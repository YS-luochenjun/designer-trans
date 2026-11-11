"""Local draft previews are separate from review, approval, and delivery."""
from threading import Lock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from video_translation.application.local_preview import create_preview
from video_translation.domain.production import CleanAction


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    locale: str


class LocalCleanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: CleanAction


def preview_routes(service, worker):
    router = APIRouter(prefix="/api")
    rendering = Lock()

    @router.post("/tasks/{task_id}/local-clean-candidate")
    def local_clean(task_id: str, body: LocalCleanRequest):
        from video_translation.application.local_candidate import create_local_candidate
        if not rendering.acquire(blocking=False):
            raise HTTPException(409, "本地媒体正在生成，请等待本轮完成")
        try:
            with worker.guard(task_id):
                result = create_local_candidate(
                    service, task_id, body.action.model_dump(),
                    expected_base_sha256=body.expected_base_sha256,
                )
            return {**result, "scheduled_new_calls": False, "pause_preserved": True}
        finally:
            rendering.release()

    @router.post("/tasks/{task_id}/local-preview")
    def preview(task_id: str, body: PreviewRequest):
        if not rendering.acquire(blocking=False):
            raise HTTPException(409, "本地草稿正在生成，请等待本轮完成")
        try:
            with worker.guard(task_id):
                result = create_preview(service, task_id, body.asset_id, body.locale)
            return {key: result[key] for key in (
                "id", "artifact_id", "quality_passed", "rendered_layers", "omitted_layers", "limitations")}
        finally:
            rendering.release()

    return router
