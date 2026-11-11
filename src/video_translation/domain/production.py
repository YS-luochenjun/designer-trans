"""Contracts for evidence-driven, local production decisions (never shell commands)."""
from typing import Literal
from pydantic import Field, model_validator
from .models import Contract, Region
from .local_repair import SurfaceRepair, TrackedDonorRepair


class MotionPoint(Contract):
    time_ms: int = Field(ge=0)
    x: float = Field(default=0, ge=-16384, le=16384)
    y: float = Field(default=0, ge=-16384, le=16384)
    scale: float = Field(default=1, gt=0, le=8)
    opacity: float = Field(default=1, ge=0, le=1)


class LetterStyle(Contract):
    font_id: str = Field(default="default", max_length=120)
    font_size: int = Field(ge=8, le=400)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    outline_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    outline_width: int = Field(default=0, ge=0, le=20)
    shadow_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    shadow_x: int = Field(default=0, ge=-40, le=40)
    shadow_y: int = Field(default=0, ge=-40, le=40)
    align: Literal["left", "center", "right"] = "center"


class MaskPoint(Contract):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class OcclusionFrame(Contract):
    time_ms: int = Field(ge=0)
    points: list[MaskPoint] = Field(min_length=3, max_length=32)


class SourceLettering(Contract):
    source_id: str
    appearance: str = Field(min_length=1, max_length=1600)
    motion_description: str = Field(min_length=1, max_length=1600)
    motion_status: Literal["unknown", "static", "dynamic"] = "unknown"
    motion_evidence_times_ms: list[int] = Field(default_factory=list, max_length=32)
    background_description: str = Field(min_length=1, max_length=1600)
    protected_content: list[str] = Field(default_factory=list, max_length=20)
    evidence_times_ms: list[int] = Field(min_length=1, max_length=12)
    style: LetterStyle
    motion: list[MotionPoint] = Field(default_factory=list, max_length=32)
    speech_id: str | None = None
    needs_occlusion: bool = False
    occlusion: list[OcclusionFrame] = Field(default_factory=list, max_length=32)
    uncertainty: str = Field(default="", max_length=1200)


class SourceDesign(Contract):
    lettering: list[SourceLettering] = Field(default_factory=list, max_length=200)
    scene_boundaries_ms: list[int] = Field(default_factory=list, max_length=200)
    observations: list[str] = Field(default_factory=list, max_length=30)


class CleanFinding(Contract):
    category: Literal["readable_source_text", "subject_damage", "new_object", "severe_flicker",
                      "edge", "texture", "brightness", "other"]
    severity: Literal["minor", "major", "uncertain"]
    frame_indices: list[int] = Field(min_length=1, max_length=24)
    observation: str = Field(min_length=1, max_length=800)
    viewing_impact: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def nonnegative_frames(self):
        if any(i < 0 for i in self.frame_indices):
            raise ValueError("Finding frame indices must be nonnegative")
        return self


class CleanQuality(Contract):
    text_removal: int = Field(ge=0, le=40)
    subject_preservation: int = Field(ge=0, le=40)
    temporal_consistency: int = Field(ge=0, le=20)
    hard_failures: list[Literal["readable_source_text", "subject_damage", "new_object", "severe_flicker"]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list, max_length=10)
    findings: list[CleanFinding] = Field(default_factory=list, max_length=12)


class CleanAction(Contract):
    action: Literal["inspect", "track", "inspect_pipeline", "inspect_archive", "rebase_candidate", "defer", "reject_candidate", "consult", "revise_scope", "confirm_scope", "restore_candidate", "repair", "request_reference", "accept", "finish", "blocked"]
    reason: str = Field(min_length=1, max_length=2400)
    process_findings: list[str] = Field(default_factory=list, max_length=5)
    source_id: str | None = None
    times_ms: list[int] = Field(default_factory=list, max_length=12)
    method: Literal["flat", "donor", "image_edit", "wan", "local_surface", "tracked_donor", "reference_plate"] | None = None
    reference_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    context_start_ms: int | None = Field(default=None, ge=0)
    context_end_ms: int | None = Field(default=None, gt=0)
    region: Region | None = None
    background: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    donor_time_ms: int | None = Field(default=None, ge=0)
    donor_region: Region | None = None
    prompt: str = Field(default="", max_length=6000)
    candidate_id: str | None = None
    evidence: list[str] = Field(default_factory=list, max_length=30)
    knowledge_ids: list[str] = Field(default_factory=list, max_length=12)
    quality: CleanQuality | None = None
    review_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_source_ids: list[str] = Field(default_factory=list, max_length=200)
    absent_times_ms: list[int] = Field(default_factory=list, max_length=12)
    surface_repair: SurfaceRepair | None = None
    tracked_donor: TrackedDonorRepair | None = None
    protected_foreground: list[OcclusionFrame] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def coherent(self):
        if self.action == "request_reference" and (
                not self.source_id or len(self.times_ms) != 1 or not self.prompt or not self.evidence):
            raise ValueError("Reference request requires source, one observed time, prompt and evidence")
        if self.action == "inspect" and not self.times_ms:
            raise ValueError("检查必须指定实际时间点")
        if self.action == "repair" and (not self.source_id or not self.method):
            raise ValueError("修复必须绑定文字实例及工具")
        if self.action == "repair" and self.method == "reference_plate" and not self.reference_id:
            raise ValueError("Generated-reference repair requires an admitted reference ID")
        if self.action == "repair" and self.method == "local_surface" and self.surface_repair is None:
            raise ValueError("Local surface repair requires observed keyframes and evidence")
        if self.action == "repair" and self.method == "tracked_donor" and (
            self.tracked_donor is None or self.donor_time_ms is None or self.donor_region is None
        ):
            raise ValueError("Tracked donor requires observed geometry, clean donor time and region")
        if self.action == "track" and (not self.source_id or self.start_ms is None or self.end_ms is None
                                       or self.region is None or not self.evidence):
            raise ValueError("Tracking requires source ID, observed region, time range and evidence")
        if self.action == "restore_candidate" and not self.candidate_id:
            raise ValueError("复用候选必须指定已归档候选ID")
        if self.action == "inspect_pipeline" and (not (self.candidate_id or self.source_id) or not self.evidence):
            raise ValueError("只读链路诊断必须指定候选ID或文字实例及观察证据")
        if self.action in {"inspect_archive", "rebase_candidate", "reject_candidate"} and (
            not self.candidate_id or not self.evidence
        ):
            raise ValueError("候选诊断或拒绝必须绑定候选ID和观察证据")
        if self.action == "defer" and (not self.source_id or not self.evidence):
            raise ValueError("暂缓实例必须绑定文字实例和障碍证据")
        if self.action in {"defer", "blocked"}:
            if (self.start_ms is None) != (self.end_ms is None):
                raise ValueError("Deferred range requires both start_ms and end_ms")
            if self.start_ms is not None and self.start_ms >= self.end_ms:
                raise ValueError("Deferred range must be ordered and nonempty")
        if self.action == "revise_scope":
            if not self.source_id or not self.times_ms or not self.evidence:
                raise ValueError("修订范围必须指定文字实例、已观察时间点与证据")
            if (self.start_ms is None) != (self.end_ms is None):
                raise ValueError("修订时段必须同时指定起止时间")
            if self.start_ms is None and self.region is None:
                raise ValueError("修订范围必须指定时段或区域；仅指定区域时保留当前时段")
        if (self.context_start_ms is None) != (self.context_end_ms is None):
            raise ValueError("视频上下文起止时间必须同时提供")
        if self.action == "accept" and (not self.candidate_id or not self.evidence):
            raise ValueError("接受候选必须绑定候选身份和实际观察")
        if self.action == "confirm_scope" and (not self.candidate_id or not self.evidence or self.quality is None):
            raise ValueError("范围复核须绑定已有接受记录、实际帧证据和质量评分")
        if self.action == "finish" and not self.evidence:
            raise ValueError("完成清底必须提供对照观察")
        return self


class LetteringPlan(Contract):
    source_id: str
    style: LetterStyle
    motion: list[MotionPoint] = Field(default_factory=list, max_length=32)
    treatment: Literal["font", "artwork"] = "font"
    artwork_prompt: str = Field(default="", max_length=4000)
    rationale: str = Field(min_length=1, max_length=1600)


class SpeakerVoice(Contract):
    speaker: str = Field(min_length=1, max_length=120)
    voice: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)


class AdaptivePlan(Contract):
    lettering: list[LetteringPlan] = Field(default_factory=list, max_length=200)
    speakers: list[SpeakerVoice] = Field(default_factory=list, max_length=50)
    preserve_music: bool = True
    notes: list[str] = Field(default_factory=list, max_length=30)


class LetteringDecision(Contract):
    action: Literal["accept", "revise", "inspect", "blocked"]
    reason: str = Field(min_length=1, max_length=2400)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: list[str] = Field(min_length=1, max_length=30)
    times_ms: list[int] = Field(default_factory=list, max_length=12)
    revisions: list[LetteringPlan] = Field(default_factory=list, max_length=50)


class SpeechRevision(Contract):
    source_id: str
    text: str = Field(min_length=1, max_length=8000)
    reason: str = Field(min_length=1, max_length=1600)


class CleanRouteDecision(Contract):
    action: Literal["assign_work", "blocked"]
    reason: str = Field(min_length=1, max_length=2400)
    source_id: str | None = None
    method: Literal["local_surface", "tracked_donor", "inspect", "image_reference", "reference_plate"] | None = None
    question: str = Field(default="", max_length=2400)
    evidence: list[str] = Field(min_length=1, max_length=20)


PRODUCTION_CONTRACTS = {"agent/source-design": SourceDesign, "agent/clean-action": CleanAction,
                        "agent/clean-route": CleanRouteDecision,
                        "agent/adaptive-plan": AdaptivePlan, "agent/lettering-decision": LetteringDecision,
                        "agent/speech-revision": SpeechRevision}
