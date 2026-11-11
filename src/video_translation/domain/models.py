"""Validated business contracts; JSON Schema is exported from these models."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .management import ManagerDecision, ManagerSettings
from .execution import EXECUTION_CONTRACTS, ExecutionMode
from .repair_evaluation import EvaluationRequest
from .intake_policy import IntakePolicy
from .rework import ReworkRequest
from .route_recovery import RouteAutonomy, RouteRecoveryDecision
from .visual_recovery import VisualRecoveryAuthorization


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeStatus(StrEnum):
    pending = "pending"
    queued = "queued"
    running = "running"
    waiting_external = "waiting_external"
    waiting_user = "waiting_user"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"
    interrupted = "interrupted"
    unknown = "unknown"


class ExternalLimits(Contract):
    max_kie_credits: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_paid_calls: int | None = Field(default=None, ge=0)
    max_video_credits: int | None = Field(default=None, ge=0)
    max_image_calls: int | None = Field(default=None, ge=0)
    max_visual_steps: int | None = Field(default=None, ge=4)


class TaskCreate(ExternalLimits):
    title: str = Field(min_length=1, max_length=200)
    asset_ids: list[str] = Field(min_length=1, max_length=20)
    target_locales: list[str] = Field(min_length=1, max_length=12)
    source_language: str | None = Field(default=None, max_length=35)
    notes: str = Field(default="", max_length=8000)
    confirm_brief: bool = False
    profile: Literal["default"] = "default"
    max_revisions: int = Field(default=1, ge=0, le=3)
    visual_route: Literal["agent_adaptive", "solid_overlay", "wan_video_edit"] = "agent_adaptive"
    audio_strategy: Literal["preserve_background", "replace_all_audio"] = "preserve_background"
    allow_review_draft: bool = False
    execution_profile: ExecutionMode = "efficient_v1"

    @model_validator(mode="after")
    def draft_route(self):
        if self.allow_review_draft and self.visual_route != "agent_adaptive":
            raise ValueError("Review drafts require the adaptive production workflow")
        if self.allow_review_draft and len(self.asset_ids) != 1:
            raise ValueError("Review drafts currently support one source video with multiple locales")
        return self

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()

    @field_validator("asset_ids", "target_locales")
    @classmethod
    def unique_items(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("列表不得重复")
        return value

    @field_validator("target_locales")
    @classmethod
    def locales(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", item) for item in value):
            raise ValueError("语言使用规范标签，例如 ja-JP、de-DE、en")
        return value


class TimedItem(Contract):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered_time(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms 必须晚于 start_ms")
        return self


class Region(Contract):
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_frame(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("区域越出画面")
        return self


class TextItem(TimedItem):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    region: Region
    treatment: Literal["translate", "preserve"] = "translate"
    reason: str = ""


class SpeechItem(TimedItem):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=8000)
    speaker: str = "narrator"


class Inventory(Contract):
    summary: str
    source_language: str
    texts: list[TextItem] = Field(default_factory=list, max_length=200)
    speech: list[SpeechItem] = Field(default_factory=list, max_length=200)
    has_music: bool
    preserve_music: bool = True
    unknowns: list[str] = Field(default_factory=list, max_length=30)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in [*self.texts, *self.speech]]
        if len(ids) != len(set(ids)):
            raise ValueError("内容清单ID重复")
        return self


class TranslationItem(Contract):
    source_id: str
    text: str = Field(min_length=1, max_length=8000)


class TranslationPackage(Contract):
    locale: str
    items: list[TranslationItem]
    notes: list[str] = Field(default_factory=list)


class VisualStep(Contract):
    source_id: str
    route: Literal["solid_overlay", "external_clean_plate", "wan_video_edit"]
    background: str = Field(default="#101820", pattern=r"^#[0-9a-fA-F]{6}$")
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    font_size: int = Field(default=32, ge=8, le=240)
    reason: str


class ProductionPlan(Contract):
    visual_steps: list[VisualStep] = Field(default_factory=list)
    voice: str = "default"
    preserve_music: bool = True
    notes: list[str] = Field(default_factory=list)


class Defect(Contract):
    severity: Literal["critical", "major", "minor"]
    message: str
    start_ms: int | None = Field(default=None, ge=0)
    source_id: str | None = None
    evidence: str


class ReviewReport(Contract):
    verdict: Literal["pass", "needs_revision", "needs_human"]
    covered_ids: list[str]
    defects: list[Defect] = Field(default_factory=list)
    evidence: list[str]
    limitations: list[str] = Field(default_factory=list)


class Answer(Contract):
    answer: str = Field(min_length=1, max_length=10000)
    feedback: str = Field(default="", max_length=4000)


class ScheduleCreate(Contract):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["project_patrol", "requirements_check", "agent_message"] = "project_patrol"
    trigger: Literal["interval", "once"]
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    run_at: datetime | None = None
    timezone: str = "Asia/Shanghai"
    enabled: bool = True
    prompt: str = Field(default="", max_length=4000)
    refresh_requirements: bool = False

    @field_validator("prompt")
    @classmethod
    def clean_prompt(cls, value):
        return value.strip()

    @model_validator(mode="after")
    def valid_schedule(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("无效IANA时区") from exc
        if self.trigger == "interval" and (not self.interval_seconds or self.run_at is not None):
            raise ValueError("间隔计划需interval_seconds，不能同时设置run_at")
        if self.trigger == "once" and (self.run_at is None or self.run_at.tzinfo is None):
            raise ValueError("一次性计划需带时区的run_at")
        if self.trigger == "once" and self.interval_seconds is not None:
            raise ValueError("一次性计划不能同时设置interval_seconds")
        if self.kind == "agent_message" and not self.prompt:
            raise ValueError("自定义轮询必须填写发给管理 Agent 的消息")
        if self.kind != "agent_message" and (self.prompt or self.refresh_requirements):
            raise ValueError("仅自定义轮询支持消息和执行前刷新需求")
        return self


CONTRACTS = {
    **EXECUTION_CONTRACTS,
    "execution/repair-evaluation": EvaluationRequest,
    "workflow/intake-policy": IntakePolicy,
    "workflow/directed-rework": ReworkRequest,
    "workflow/route-autonomy": RouteAutonomy,
    "workflow/visual-recovery-authorization": VisualRecoveryAuthorization,
    "agent/route-recovery": RouteRecoveryDecision,
    "tasks/task-create": TaskCreate,
    "agent/inventory": Inventory,
    "agent/translation-package": TranslationPackage,
    "agent/production-plan": ProductionPlan,
    "agent/review-report": ReviewReport,
    "workflow/schedule": ScheduleCreate,
    "agent/manager-decision": ManagerDecision,
    "workflow/manager-settings": ManagerSettings,
}
