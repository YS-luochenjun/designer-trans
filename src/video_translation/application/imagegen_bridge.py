"""Guarded host-tool claims and receipts; no model calls in the HTTP server."""
from pathlib import Path
from secrets import token_hex

from video_translation.application.clean_reference import stopped_clean, register_reference
from video_translation.application.tasks import Conflict
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from video_translation.workflow.graph import task_status


def public_requests(service, task_id):
    """Separate queue state from actual tool dispatch without claim credentials."""
    result = []
    for record in service.store.list("imagegen_request"):
        if record["task_id"] != task_id:
            continue
        row = {k: v for k, v in record.items() if k != "claim_token"}
        status = record["status"]
        row["lifecycle"] = (
            "dispatch_blocked" if status == "pending" and record.get("dispatch_blocked_reason")
            else "queued" if status == "pending"
            else "needs_reconciliation" if status == "claimed"
            and record.get("execution_state") in {"unknown", "receipt_invalid"}
            else "claimed" if status == "claimed"
            else status)
        row["expected_executor"] = "project_codex_worker"
        reference = service.store.get("clean_reference", record.get("reference_id", ""))
        if reference:
            row["generated_image_path"] = reference["path"]
        result.append(row)
    return sorted(result, key=lambda row: row["created_at"], reverse=True)


def context(service, task_id, request_id):
    record = service.store.require("imagegen_request", request_id)
    if record["task_id"] != task_id:
        raise Conflict("Request belongs to another task")
    task, state, node, controller = stopped_clean(
        service, task_id, record["base_sha256"], request_id)
    if (state.get("imagegen_request_id") != request_id or node.get("tool_request_id") != request_id
            or service.hash_file(controller.source) != record["source_sha256"]):
        raise Conflict("Reconcile the source-bound request first")
    original = Path(record["original_frame_path"])
    root = (service.settings.runtime / "tasks" / task_id).resolve()
    if (not original.resolve().is_relative_to(root) or not original.is_file()
            or service.hash_file(original) != record["original_sha256"]):
        raise Conflict("Imagegen input frame changed")
    return record, task, state, node


def claim(service, task_id, request_id):
    record, _, _, _ = context(service, task_id, request_id)
    if record["status"] != "pending":
        raise Conflict("Request already claimed or completed; never replay an uncertain generation")
    claimed = {**record, "status": "claimed", "claim_token": token_hex(24), "claimed_at": now()}
    claimed.pop("dispatch_blocked_reason", None)
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if tx.require("imagegen_request", request_id) != record:
            raise Conflict("Request was claimed concurrently")
        tx.put("imagegen_request", claimed)
    return claimed


def complete(service, task_id, request_id, claim_token, generated_image_path=None, error=None):
    record, task, state, node = context(service, task_id, request_id)
    if record["status"] != "claimed" or record.get("claim_token") != claim_token:
        raise Conflict("Receipt must match the original claim")
    if bool(generated_image_path) == bool(error):
        raise Conflict("Supply exactly one generated file or confirmed tool failure")
    if generated_image_path:
        return register_reference(service, task_id, record["source_id"], record["base_sha256"],
                                  record["frame_index"], record["original_frame_path"],
                                  generated_image_path, request_record=record)
    # A confirmed tool failure returns the work to routing/draft, never a blind new request.
    from copy import deepcopy
    original_task, original_state = deepcopy(task), deepcopy(state)
    state.setdefault("deferred", {})[record["source_id"]] = {
        "reason": "Built-in imagegen failed: " + error[:1200], "turn": state["turn"]}
    state.pop("imagegen_request_id", None)
    node.update(status="pending", output={}, tool_request_id=None, error=None, finished_at=None)
    task.update(status=task_status(task), updated_at=now())
    with service.store.connection() as db:
        tx = TransactionStore(service.store, db)
        if (tx.require("imagegen_request", request_id) != record
                or tx.require("task", task_id) != original_task
                or tx.require("production_session", state["id"]) != original_state):
            raise Conflict("Task changed during failure receipt")
        tx.put("imagegen_request", {**record, "status": "failed", "error": error[:1200], "finished_at": now()})
        tx.put("production_session", state)
        tx.put("task", task)
    return {"task_id": task_id, "status": task["status"], "generation_retried": False}
