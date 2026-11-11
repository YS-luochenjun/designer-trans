"""Explicit, observed geometry for local repair; no automatic tracking claims."""
from typing import Literal
from pydantic import Field, model_validator

from .models import Contract, Region


class RepairKeyframe(Contract):
    time_ms: int = Field(ge=0)
    region: Region


class SurfaceRepair(Contract):
    model: Literal["linear", "plane"] = "linear"
    axis: Literal["horizontal", "vertical"] = "horizontal"
    keyframes: list[RepairKeyframe] = Field(min_length=1, max_length=64)
    evidence: list[str] = Field(min_length=1, max_length=16)
    max_edge_difference: int = Field(default=32, ge=0, le=64)

    @model_validator(mode="after")
    def ordered(self):
        times = [key.time_ms for key in self.keyframes]
        if times != sorted(set(times)):
            raise ValueError("Repair keyframes must be strictly ordered")
        return self


class TrackedDonorRepair(Contract):
    """Observed target geometry; the clean donor belongs to the same shot."""
    keyframes: list[RepairKeyframe] = Field(min_length=2, max_length=600)
    evidence: list[str] = Field(min_length=1, max_length=16)
    scene_start_ms: int = Field(ge=0)
    scene_end_ms: int = Field(gt=0)
    max_border_error: int = Field(default=20, ge=0, le=40)

    @model_validator(mode="after")
    def ordered(self):
        times = [key.time_ms for key in self.keyframes]
        if times != sorted(set(times)) or self.scene_start_ms >= self.scene_end_ms:
            raise ValueError("Tracked donor requires ordered keyframes and one observed shot")
        return self
