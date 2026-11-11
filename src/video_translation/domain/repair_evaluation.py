"""Engine-neutral, isolated evaluation contracts; no production capability grant."""
from fractions import Fraction
from typing import Literal

from pydantic import Field, model_validator

from .execution import ExecutionContract, FileRef


class FrameMask(ExecutionContract):
    frame_index: int = Field(ge=0)
    removal: FileRef
    protection: FileRef | None = None


class EvaluationRequest(ExecutionContract):
    revision: Literal[1] = 1
    mode: Literal["isolated_evaluation"] = "isolated_evaluation"
    source: FileRef
    base: FileRef
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_frame: int = Field(ge=0)
    frame_count: int = Field(gt=0, le=300)
    context_first: int = Field(ge=0)
    context_count: int = Field(gt=0, le=300)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    rate: str
    engine: str = Field(min_length=1, max_length=100)
    engine_version: str = Field(min_length=1, max_length=100)
    weights_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    usage_review: Literal["evaluation_only"] = "evaluation_only"
    timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    max_output_bytes: int = Field(default=64 * 1024**2, gt=0, le=512 * 1024**2)
    max_decoded_bytes: int = Field(default=128 * 1024**2, gt=0, le=512 * 1024**2)
    masks: list[FrameMask] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def coherent(self):
        if self.base.sha256 != self.expected_base_sha256:
            raise ValueError("Evaluation base is stale")
        if not 0 < Fraction(self.rate) <= 120 or self.width * self.height > 8_294_400:
            raise ValueError("Unsupported evaluation geometry or rate")
        stop = self.first_frame + self.frame_count
        if not self.context_first <= self.first_frame < stop <= self.context_first + self.context_count:
            raise ValueError("Context must contain the exact edit frame window")
        if [m.frame_index for m in self.masks] != list(range(self.first_frame, stop)):
            raise ValueError("Exactly one ordered removal mask is required for each edit frame")
        estimate = self.width * self.height * (self.context_count * 7 + self.frame_count * 2)
        if estimate > self.max_decoded_bytes:
            raise ValueError("Evaluation decoded-frame estimate exceeds its memory allowance")
        return self
