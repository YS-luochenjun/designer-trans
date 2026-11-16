from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from video_translation.application import facefusion
from video_translation.domain.facefusion import FaceFusionConfigure, FaceFusionProfile
from video_translation.integrations.facefusion import public_status


class TargetPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    profile: FaceFusionProfile = Field(default_factory=FaceFusionProfile)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def facefusion_routes(service, worker):
    router = APIRouter(prefix="/api")

    @router.get("/facefusion/status")
    def status():
        return public_status()

    @router.post("/facefusion/target-preview")
    def preview(body: TargetPreview):
        return facefusion.preview(service, body.asset_id, body.profile)

    @router.get("/facefusion/previews/{key}/files/{index}")
    def image(key: str, index: int):
        record = service.store.require("face_preview", key)
        if index < 0 or index >= len(record["files"]):
            raise KeyError(index)
        file = record["files"][index]
        path = service.checked_path(file)
        if service.hash_file(path) != file["sha256"]:
            raise ValueError("目标脸参考图片发生变化")
        return FileResponse(path, media_type="image/png")

    @router.put("/tasks/{task_id}/facefusion")
    def configure(task_id: str, body: FaceFusionConfigure):
        with worker.guard(task_id):
            return facefusion.configure(service, task_id, body)

    @router.post("/tasks/{task_id}/facefusion/approve")
    def approve(task_id: str, body: Approval):
        with worker.dispatch_lock:
            return facefusion.approve(service, task_id, body.sha256)

    return router
