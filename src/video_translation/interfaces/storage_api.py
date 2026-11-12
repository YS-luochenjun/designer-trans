"""Historical retirement uses explicit plans; manager questions never delete files."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from video_translation.application.task_storage import apply_history_plan, history_plan
from video_translation.domain.execution import identity


class RetirementPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_recent_per_source: int = Field(default=2, ge=1, le=20)


class RetirementApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approve_historical_pixels: bool = False


def storage_routes(service, worker):
    router = APIRouter(prefix="/api")

    @router.get("/tasks/{task_id}/history-retirement")
    def preview(task_id: str):
        plan = history_plan(service, task_id, hash_candidates=False)
        return {key: value for key, value in plan.items() if key != "files"} | {
            "candidate_video_count": len(plan["files"]), "executable": False}

    @router.post("/tasks/{task_id}/history-retirement/plans")
    def plan(task_id: str, body: RetirementPlanRequest):
        with worker.guard(task_id):
            result = history_plan(service, task_id, keep_recent_per_source=body.keep_recent_per_source)
            key = identity([task_id, result["plan_sha256"]])
            return service.store.put("storage_plan", {
                "id": key, "task_id": task_id, "created_at": result["created_at"], "plan": result})

    @router.post("/history-retirement/{plan_id}/apply")
    def apply(plan_id: str, body: RetirementApproval):
        from video_translation.application.tasks import Conflict
        plan = service.store.require("storage_plan", plan_id)["plan"]
        if body.expected_plan_sha256 != plan["plan_sha256"]:
            raise Conflict("Approval does not match the retained retirement plan")
        receipt = apply_history_plan(service, plan, approve=body.approve_historical_pixels, guard=worker.guard)
        return {key: receipt[key] for key in (
            "id", "task_id", "status", "bytes_removed", "disk_free_before", "disk_free_after")}

    @router.get("/history-retirement/receipts/{receipt_id}")
    def receipt(receipt_id: str):
        return service.store.require("storage_retirement", receipt_id)

    return router
