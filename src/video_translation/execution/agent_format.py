"""One separately recorded formatting request after a confirmed invalid reply."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from video_translation.application.tasks import Conflict
from video_translation.integrations.agents.errors import (
    InvalidStructuredAnswer, ProviderError, UnknownExternalResult,
)
from video_translation.persistence.store import now
from video_translation.persistence.transaction import TransactionStore
from .work_autonomy import enabled


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _retained(record, request_sha256, schema):
    if record["request_sha256"] != request_sha256:
        raise Conflict("Format recovery belongs to a different work packet")
    if record["status"] == "succeeded":
        result = deepcopy(record["result"])
        Draft202012Validator(schema).validate({k: v for k, v in result.items() if k != "_media"})
        return result
    if record["status"] in {"running", "unknown"}:
        raise UnknownExternalResult("格式修正原请求结果待核对，不重复提交")
    raise ProviderError("本轮方案格式修正未通过；已保留两次回执，不继续循环请求")


def run_agent(executor, agent, task, node, packet, schema, directory):
    decision_id = packet.get("decision_id")
    eligible = (enabled(task) and node["definition_id"] == "clean" and decision_id
                and packet.get("inputs", {}).get("agent_role") == "work")
    if not eligible:
        def run():
            if node["definition_id"] == "visual" and node.get("visual_recovery_authorization_id"):
                from video_translation.application.visual_recovery import before_model_call
                before_model_call(executor.service, task, node, decision_id)
            return agent.run(node["definition_id"], packet, schema, directory)
        return executor.paid(task, node, "agent", node["definition_id"],
                             run)
    identity = _digest({"task": task["id"], "node": node["id"], "decision": decision_id})
    request_sha = _digest({"packet": packet, "schema": schema})
    existing = executor.store.get("agent_format_recovery", identity)
    if existing:
        return _retained(existing, request_sha, schema)
    try:
        return executor.paid(task, node, "agent", node["definition_id"],
                             lambda: agent.run(node["definition_id"], packet, schema, directory))
    except InvalidStructuredAnswer as exc:
        if not eligible:
            raise
        recovery = Path(exc.recovery_path)
        if recovery.is_symlink() or not recovery.resolve().is_relative_to(Path(directory).resolve()):
            raise ProviderError("格式错误回执路径不匹配，保留原记录") from None
        answer = recovery / "answer.json"
        if answer.is_symlink() or not answer.is_file() or answer.stat().st_size > 65536:
            raise ProviderError("原方案缺失或过大，不自动请求格式修正") from None
        raw = answer.read_bytes()
        receipt = json.loads((recovery / "receipt.json").read_text("utf-8"))
        original_schema = json.loads((recovery / "request.json").read_text("utf-8"))["schema"]
        if (original_schema != schema or receipt.get("status") != "invalid_response"
                or receipt.get("reason") != "invalid_structured_answer"
                or receipt.get("cli_exit_code") != 0
                or hashlib.sha256(raw).hexdigest() != exc.answer_sha256
                or receipt.get("answer_sha256") != exc.answer_sha256):
            raise ProviderError("原请求没有已完成的无效方案证据，不自动修正") from None
        original_text, original_media = raw.decode("utf-8"), deepcopy(exc.media)
        original_sha = exc.answer_sha256
    current = executor.store.get("task", task["id"]) or task
    if current.get("control_paused") or current.get("status") == "cancelled":
        raise Conflict("任务已暂停或取消，不发送格式修正请求")
    record = {
        "id": identity, "task_id": task["id"], "node_id": node["id"],
        "decision_id": decision_id, "request_sha256": request_sha,
        "original_answer_sha256": original_sha, "original_recovery_path": str(recovery),
        "correction_decision_id": decision_id + "-format-1",
        "status": "running", "created_at": now(), "cost_status": "unknown",
    }
    with executor.store.connection() as db:
        tx = TransactionStore(executor.store, db)
        previous = tx.get("agent_format_recovery", identity)
        if previous:
            return _retained(previous, request_sha, schema)
        tx.put("agent_format_recovery", record)
    correction = {
        **packet, "decision_id": record["correction_decision_id"], "images": [],
        "inputs": {
            "agent_role": "format_repair", "original_answer": original_text,
            "original_answer_sha256": original_sha,
        },
        "instruction": (
            "The prior CLI completed, but its final business JSON was invalid. "
            "Correct only JSON serialization/schema errors in the supplied original_answer data. "
            "Do not execute tools, create media, choose a new repair, add evidence, change a score, "
            "or claim to have viewed images. Preserve the original proposal's values and intent. "
            "If the intended values cannot be established, return action=blocked with an honest reason. "
            "Return one complete business object matching the supplied original schema."
        ),
    }
    try:
        result = executor.paid(
            task, node, "agent", "agent_format_repair",
            lambda: agent.run(node["definition_id"], correction, schema, directory))
        Draft202012Validator(schema).validate({k: v for k, v in result.items() if k != "_media"})
        original_media["format_repair"] = {
            "correction_decision_id": record["correction_decision_id"],
            "original_answer_sha256": original_sha, "original_recovery_path": str(recovery),
            "correction_images_sent": result.get("_media", {}).get("images_sent", 0),
            "tool_profile": "structured",
        }
        result["_media"] = original_media
    except Exception as exc:
        failure_status = "unknown" if isinstance(exc, UnknownExternalResult) else "failed"
        failure_message = str(exc)[:1000]
        executor.store.update("agent_format_recovery", identity, lambda row: row.update(
            status=failure_status, error=failure_message, finished_at=now()))
        raise
    executor.store.update("agent_format_recovery", identity, lambda row: row.update(
        status="succeeded", result=result, finished_at=now()))
    return result
