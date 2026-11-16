"""Parameters from the locally integrated FaceFusion CLI contract."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FaceFusionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    face_selector_mode: Literal["reference", "many", "one"] = "reference"
    face_selector_order: Literal[
        "left-right", "right-left", "top-bottom", "bottom-top",
        "small-large", "large-small", "best-worst", "worst-best",
    ] = "large-small"
    reference_frame_number: int = Field(default=0, ge=0)
    reference_face_position: int = Field(default=0, ge=0)
    reference_face_distance: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)
    face_mask_types: list[Literal["box", "occlusion", "region"]] = Field(
        default_factory=lambda: ["box"], min_length=1, max_length=3)
    face_mask_blur: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)
    output_video_quality: int = Field(default=80, ge=0, le=100)
    target_preview_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_validator("reference_face_distance", "face_mask_blur")
    @classmethod
    def cli_steps(cls, value):
        if abs(value * 20 - round(value * 20)) > 1e-6:
            raise ValueError("FaceFusion 参数以 0.05 为步长")
        return value

    @field_validator("face_mask_types")
    @classmethod
    def unique_masks(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("遮罩类型不得重复")
        return value


class FaceFusionConfigure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    profile: FaceFusionProfile
