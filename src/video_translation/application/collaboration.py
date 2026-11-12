"""Durable node conversations and explicit, revision-bound production instructions."""
from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from video_translation.application.tasks import Conflict
from video_translation.domain.models import Contract
from video_translation.persistence.store import now
from video_translation.workflow.graph import task_status


class MessageCreate(Contract):
    request_id: UUID
    text: str = Field(min_length=1, max_length=4000)
    scope: Literal["node", "downstream"] = "node"
    pause_after_current: bool = True
    interaction: Literal["proposal", "route"] = "proposal"

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value.strip()


class CoordinationReply(Contract):
    reply: str = Field(min_length=1, max_length=6000)
    question: str | None = Field(default=None, max_length=1500)
    proposed_instruction: str | None = Field(default=None, max_length=4000)
    action: Literal["inform", "direct", "clarify"] = "inform"
    options: list[str] = Field(default_factory=list, max_length=4)
    target_node_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def actionable(self):
        if self.action == "direct" and not (self.proposed_instruction or "").strip():
            raise ValueError("A direction needs a concrete business instruction")
        if self.action == "clarify" and (
                not self.question or len(set(self.options)) < 2
                or any(not x.strip() or len(x) > 300 for x in self.options)):
            raise ValueError("A user decision needs a question and 2..4 distinct short options")
        return self


class CleanContinuation(Contract):
    expected_revision: int = Field(ge=0)
    expected_attempt: int = Field(ge=0)
    expected_status: Literal["failed", "interrupted", "succeeded"]
    expected_output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def node_for(task, node_id):
    return next((node for node in task["nodes"] if node["id"] == node_id), None)


def affected_nodes(task, node_id, scope):
    affected = {node_id}
    if scope == "downstream":
        changed = True
        while changed:
            previous = len(affected)
            affected.update(n["id"] for n in task["nodes"] if set(n["depends_on"]) & affected)
            changed = len(affected) != previous
    return affected


def instructions_for(task, node):
    return [rule for rule in task.get("collaboration_rules", [])
            if node["id"] in affected_nodes(task, rule["node_id"], rule["scope"])]


def basis(task, node_id, scope):
    ids = affected_nodes(task, node_id, scope)
    value = {"revision": task.get("brief_revision", 0),
             "nodes": [{key: n.get(key) for key in ("id", "attempt", "status", "output")}
                       for n in task["nodes"] if n["id"] in ids]}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Collaboration:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def list(self, task_id, node_id=None):
        self.store.require("task", task_id)
        return sorted((m for m in self.store.list("conversation") if m["task_id"] == task_id
                       and (node_id is None or m["node_id"] == node_id)),
                      key=lambda m: (m["created_at"], m["id"]))

    def _editable(self, task_id):
        if self.store.get("deletion_receipt", task_id):
            raise Conflict("任务正在删除，不能接收消息或调整")
        return self.store.require("task", task_id)

    def submit(self, task_id, node_id, request: MessageCreate):
        with self.store.lock:
            task = self._editable(task_id)
            if node_for(task, node_id) is None:
                raise KeyError(node_id)
            key = request.request_id.hex
            existing = self.store.get("conversation", key)
            if existing:
                if any(existing[k] != v for k, v in {
                    "task_id": task_id, "node_id": node_id, "text": request.text,
                    "scope": request.scope, "pause_after_current": request.pause_after_current,
                }.items()):
                    raise Conflict("消息编号已用于其他内容")
                if existing.get("interaction", "proposal") != request.interaction:
                    raise Conflict("消息编号已用于其他沟通方式")
                return existing
            if sum(m["status"] in {"queued", "running"} for m in self.list(task_id)) >= 10:
                raise Conflict("待处理消息过多，请等待当前消息完成")
            if request.pause_after_current and task["status"] not in {"cancelled", "completed"}:
                self.pause(task_id, True)
            return self.store.put("conversation", {
                "id": key, "task_id": task_id, "node_id": node_id, "text": request.text,
                "scope": request.scope, "pause_after_current": request.pause_after_current,
                "interaction": request.interaction, "recipient": "route",
                "status": "queued", "created_at": now(), "reply": None, "error": None,
                "applied_revision": None,
            })

    def pause(self, task_id, paused):
        with self.store.lock:
            task = self._editable(task_id)
            if task["status"] in {"cancelled", "completed"}:
                raise Conflict("已结束任务只能讨论，不能恢复制作")
            if not paused and any(m["status"] in {"queued", "running"} for m in self.list(task_id)):
                raise Conflict("请先等待已发送的消息处理完毕")
            def mutate(current):
                current["control_paused"] = paused
                current["status"] = task_status(current)
            return self.store.update("task", task_id, mutate)

    def pending(self, task_id, *, interaction=None):
        return next((m for m in self.list(task_id) if m["status"] == "queued"
                     and (interaction is None or m.get("interaction", "proposal") == interaction)), None)

    def apply(self, task_id, message_id):
        with self.store.lock:
            task = self._editable(task_id)
            message = self.store.require("conversation", message_id)
            if message["task_id"] != task_id:
                raise KeyError(message_id)
            if message.get("applied_revision") is not None:
                return message
            if task["status"] in {"cancelled", "completed"}:
                raise Conflict("已结束任务只保留讨论；制作调整需另建任务")
            if not task.get("control_paused") or any(n["status"] == "running" for n in task["nodes"]):
                raise Conflict("请先暂停并等待当前步骤结束，再应用建议")
            proposal = (message.get("reply") or {}).get("proposed_instruction")
            if message["status"] != "succeeded" or not proposal:
                raise Conflict("没有可应用的Agent建议")
            if message.get("basis") != basis(task, message["node_id"], message["scope"]):
                raise Conflict("节点结果或要求已变化，请重新与Agent确认")
            revision = task.get("brief_revision", 0) + 1
            def mutate(current):
                current.setdefault("collaboration_rules", []).append({
                    "message_id": message_id, "node_id": message["node_id"], "scope": message["scope"],
                    "text": proposal, "revision": revision, "created_at": now(),
                })
                current["brief_revision"] = revision
            # These two records must commit together: retrying apply cannot duplicate a rule.
            with self.store.connection() as db:
                mutate(task)
                message["applied_revision"] = revision
                for kind, row in (("task", task), ("conversation", message)):
                    row["updated_at"] = now()
                    db.execute("UPDATE records SET data=? WHERE kind=? AND id=?",
                               (json.dumps(row, ensure_ascii=False), kind, row["id"]))
            return message

    def recover(self):
        for message in self.store.list("conversation"):
            if message["status"] == "running":
                self.store.update("conversation", message["id"],
                                  lambda m: m.update(status="unknown", error="服务中断，回复结果未知；未自动重发"))

    def continue_clean(self, task_id, message_id, request):
        from .node_continuation import continue_clean
        return continue_clean(self.service, task_id, message_id, request)
