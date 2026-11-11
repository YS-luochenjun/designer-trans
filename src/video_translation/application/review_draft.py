"""Record user best-effort intent without accepting unfinished media."""
from copy import deepcopy

from video_translation.application.clean_reference import stopped_clean
from video_translation.application.tasks import Conflict
from video_translation.execution.clean_progress import fingerprint
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status


def make_authorization(task_id, base_sha256=None):
    return {"id": fingerprint({"task": task_id, "base": base_sha256,
                              "intent": "user_best_effort_review"}),
            "at": now(), "base_sha256": base_sha256,
            "intent": "尽agent所能处理；轻微瑕疵不追求完美，方法耗尽后产出供人工审看的完整工作版",
            "quality_acceptance": False, "allow_new_visual_kie_calls": False,
            "image_provider": "codex_builtin_imagegen",
            "imagegen_bridge": True, "max_imagegen_requests": 12,
            "max_work_actions_per_assignment": 8}


def enable_review_draft(service, task_id, expected_base_sha256):
    task, state, node, _ = stopped_clean(service, task_id, expected_base_sha256)
    original = deepcopy(task)
    if task.get("review_draft_authorization"):
        return {"task_id": task_id, "authorization": task["review_draft_authorization"],
                "already_enabled": True, "scheduled": False}
    authorization = make_authorization(task_id, expected_base_sha256)
    task["review_draft_authorization"] = authorization
    node.update(status="pending", error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if tx.require("task", task_id) != original or tx.require("production_session", state["id"]) != state:
            raise Conflict("Task changed during draft authorization")
        tx.put("task", task)
    return {"task_id": task_id, "authorization": authorization, "scheduled": True}
