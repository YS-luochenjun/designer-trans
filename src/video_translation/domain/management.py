"""Contracts for the project supervisor; production remains owned by TaskService."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ManagementContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManagerSettings(ManagementContract):
    agent_enabled: bool = True
    max_calls_per_day: int = Field(default=20, ge=0, le=500)
    auto_pause: bool = False
    stall_minutes: int = Field(default=30, ge=5, le=1440)


class ManagerMessage(ManagementContract):
    request_id: UUID
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value.strip()


class ManagerProposal(ManagementContract):
    kind: Literal["pause_task", "resume_task", "retry_task", "sync_requirements"]
    target_id: str = Field(max_length=128)
    reason: str = Field(min_length=1, max_length=1500)


class ManagerDecision(ManagementContract):
    reply: str = Field(min_length=1, max_length=6000)
    question: str | None = Field(default=None, min_length=1, max_length=1500)
    actions: list[ManagerProposal] = Field(default_factory=list, max_length=8)


class ManagerAnswer(ManagementContract):
    answer: str = Field(min_length=1, max_length=4000)

    @field_validator("answer")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("回答不能为空")
        return value.strip()
