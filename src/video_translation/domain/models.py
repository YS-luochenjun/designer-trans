"""Validated business contracts; JSON Schema is exported from these models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .management import ManagerDecision, ManagerMessage, ManagerSettings
from .work_handoffs import HandoffRequest
from .execution import EXECUTION_CONTRACTS, ExecutionMode, ResourceBudget
from .repair_evaluation import EvaluationRequest
from .intake_policy import IntakePolicy
from .rework import ReworkRequest
from .route_recovery import RouteAutonomy, RouteRecoveryDecision
from .visual_recovery import VisualRecoveryAuthorization
from .facefusion import FaceFusionConfigure, FaceFusionProfile
from media_capabilities.locales import normalize_locales, normalize_locale


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


class TaskKind(StrEnum):
    video_translation = "video_translation"
    image_translation = "image_translation"
    face_swap = "face_swap"


class DesignProfile(Contract):
    workflow_id: TaskKind | None = None
    source_set_policy: Literal["provided_source_set", "single_video"] = "provided_source_set"
    delivery_grouping: Literal["locale_bundle", "per_locale", "single_video"] = "locale_bundle"
    review_policy: Literal[
        "source_locale_member_matrix", "sample_then_full", "video_audiovisual", "external_result"
    ] = "source_locale_member_matrix"
    sample_duration_seconds: Literal[3, 5] = 5
    face_mask: Literal["standard", "tight"] = "standard"
    preserve_audio: bool = True
    target_selection: Literal["reference_frame", "single_person"] = "reference_frame"


class ExternalLimits(Contract):
    max_kie_credits: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_paid_calls: int | None = Field(default=None, ge=0)
    max_video_credits: int | None = Field(default=None, ge=0)
    max_image_calls: int | None = Field(default=None, ge=0)
    max_visual_steps: int | None = Field(default=None, ge=4)


class TaskCreate(ExternalLimits):
    title: str = Field(min_length=1, max_length=200)
    creation_request_id: str | None = Field(
        default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    asset_ids: list[str] = Field(min_length=1, max_length=20)
    task_kind: TaskKind = TaskKind.video_translation
    target_locales: list[str] = Field(default_factory=list, max_length=12)
    target_locale_labels: list[str] = Field(default_factory=list, max_length=12)
    facefusion: FaceFusionProfile | None = None
    design_profile: DesignProfile = Field(default_factory=DesignProfile)
    source_language: str | None = Field(default=None, max_length=35)
    notes: str = Field(default="", max_length=8000)
    confirm_brief: bool = False
    profile: Literal["default"] = "default"
    max_revisions: int = Field(default=1, ge=0, le=3)
    visual_route: Literal["agent_adaptive", "solid_overlay", "wan_video_edit"] = "agent_adaptive"
    audio_strategy: Literal["preserve_background", "replace_all_audio"] = "preserve_background"
    output_goal: Literal["complete_candidate", "quality_approved"] = "complete_candidate"
    allow_review_draft: bool = False
    execution_profile: ExecutionMode = "efficient_v1"
    resource_budget: ResourceBudget = Field(default_factory=ResourceBudget)

    @model_validator(mode="before")
    @classmethod
    def remember_language_labels(cls, value):
        if isinstance(value, dict) and isinstance(value.get("target_locales"), list):
            value = {**value}
            value.setdefault("target_locale_labels", list(value["target_locales"]))
        return value

    @model_validator(mode="after")
    def draft_route(self):
        if self.design_profile.workflow_id not in {None, self.task_kind}:
            raise ValueError("design_profile.workflow_id 必须与 task_kind 一致")
        self.design_profile = self.design_profile.model_copy(
            update={"workflow_id": self.task_kind}
        )
        if self.task_kind == TaskKind.face_swap:
            self.facefusion = self.facefusion or FaceFusionProfile()
            if not self.design_profile.preserve_audio:
                raise ValueError("首版视频换脸保留原音轨")
            if self.audio_strategy != "preserve_background":
                raise ValueError("首版视频换脸保留原音轨")
            self.design_profile = self.design_profile.model_copy(update={
                "source_set_policy": "single_video", "delivery_grouping": "single_video",
                "review_policy": "external_result",
            })
        elif self.task_kind == TaskKind.image_translation:
            if self.facefusion is not None:
                raise ValueError("翻译任务不能配置 FaceFusion")
            if (
                self.design_profile.source_set_policy != "provided_source_set"
                or self.design_profile.delivery_grouping != "locale_bundle"
                or self.design_profile.review_policy != "source_locale_member_matrix"
            ):
                raise ValueError("图片翻译使用来源图组、语言包和逐成员审核")
        elif self.task_kind == TaskKind.video_translation:
            if self.facefusion is not None:
                raise ValueError("翻译任务不能配置 FaceFusion")
            self.design_profile = self.design_profile.model_copy(update={
                "delivery_grouping": "per_locale", "review_policy": "video_audiovisual",
            })
        if self.task_kind == TaskKind.face_swap and self.target_locales:
            raise ValueError("视频换脸不需要目标语言")
        if self.task_kind != TaskKind.face_swap and not self.target_locales:
            raise ValueError("翻译任务至少需要一种目标语言")
        if self.allow_review_draft and self.visual_route != "agent_adaptive":
            raise ValueError("Review drafts require the adaptive production workflow")
        if self.allow_review_draft and self.task_kind != TaskKind.video_translation:
            raise ValueError("Review drafts currently support video translation only")
        if self.allow_review_draft and len(self.asset_ids) != 1:
            raise ValueError("Review drafts currently support one source video with multiple locales")
        return self

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()

    @field_validator("asset_ids")
    @classmethod
    def unique_items(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("列表不得重复")
        return value

    @field_validator("target_locales")
    @classmethod
    def locales(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("目标语言不得重复")
        return normalize_locales(value)

    @field_validator("source_language")
    @classmethod
    def source_locale(cls, value):
        return normalize_locale(value) if value else None


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


class ImageRegion(Contract):
    id: str = Field(min_length=1, max_length=80)
    bbox: list[int] = Field(min_length=4, max_length=4)
    action: Literal["translate", "preserve"] = "translate"
    source_text: str = Field(default="", max_length=8000)
    notes: str = Field(default="", max_length=1600)

    @model_validator(mode="after")
    def valid_box(self):
        x, y, width, height = self.bbox
        if any(type(value) is not int for value in self.bbox) or min(x, y) < 0 or width <= 0 or height <= 0:
            raise ValueError("图片文字区域必须是有效的像素矩形")
        if self.action == "translate" and not self.source_text.strip():
            raise ValueError("待翻译图片区域必须包含原文")
        if self.action == "preserve" and not self.notes.strip():
            raise ValueError("保留区域必须说明原因")
        return self


class ImageInventory(Contract):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0, le=10000)
    height: int = Field(gt=0, le=10000)
    complete: bool
    observations: list[str] = Field(default_factory=list, max_length=30)
    regions: list[ImageRegion] = Field(default_factory=list, max_length=200)
    protected_regions: list[ImageRegion] = Field(default_factory=list, max_length=200)
    no_text_verified: bool = False

    @model_validator(mode="after")
    def unique_regions(self):
        ids = [item.id for item in [*self.regions, *self.protected_regions]]
        if len(ids) != len(set(ids)):
            raise ValueError("图片区域ID不得重复")
        if not self.complete:
            raise ValueError("图片分析必须明确完成或由任务Route接续")
        if not self.regions and not self.no_text_verified:
            raise ValueError("没有文字区域时必须明确核实整图无文字")
        for region in [*self.regions, *self.protected_regions]:
            if region.bbox[0] + region.bbox[2] > self.width or region.bbox[1] + region.bbox[3] > self.height:
                raise ValueError("图片区域越出画布")
        return self


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


class ImageReviewReport(Contract):
    verdict: Literal["pass", "needs_revision", "needs_human"]
    covered_ids: list[str]
    defects: list[Defect] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


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
    "tasks/facefusion-configure": FaceFusionConfigure,
    "agent/inventory": Inventory,
    "agent/translation-package": TranslationPackage,
    "agent/image-inventory": ImageInventory,
    "agent/image-review-report": ImageReviewReport,
    "agent/production-plan": ProductionPlan,
    "agent/review-report": ReviewReport,
    "workflow/schedule": ScheduleCreate,
    "agent/manager-decision": ManagerDecision,
    "workflow/manager-settings": ManagerSettings,
    "workflow/manager-message": ManagerMessage,
    "workflow/work-handoff": HandoffRequest,
}
