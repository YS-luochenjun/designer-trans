"""Versioned local execution contracts, separate from production quality verdicts."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _read_historical_mode(value):
    return "efficient_v1" if value == "legacy" else value


ExecutionMode = Annotated[Literal["efficient_v1"], BeforeValidator(_read_historical_mode)]
EXECUTION_VERSION = "unified_media_v1"


def identity(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


class ExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionProfile(ExecutionContract):
    revision: Literal[1] = 1
    mode: ExecutionMode = "efficient_v1"
    reserve_bytes: int = Field(default=128 * 1024**2, ge=0)
    max_unchanged_actions: int = Field(default=12, ge=2, le=100)

class FileRef(ExecutionContract):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FramePatch(ExecutionContract):
    frame_index: int = Field(ge=0)
    rectangle: tuple[int, int, int, int]
    image: FileRef

    @model_validator(mode="after")
    def valid_rectangle(self):
        x, y, right, bottom = self.rectangle
        if min(x, y) < 0 or right <= x or bottom <= y:
            raise ValueError("Patch rectangle must have positive area")
        return self


class PatchManifest(ExecutionContract):
    revision: Literal[1] = 1
    kind: Literal["lossless_frame_patch"] = "lossless_frame_patch"
    base: FileRef
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frames: int = Field(gt=0, le=108_000)
    rate: str
    patches: list[FramePatch]
    quality_status: Literal["unreviewed"] = "unreviewed"

    @model_validator(mode="after")
    def valid_frames(self):
        from fractions import Fraction
        rate = Fraction(self.rate)
        if not 0 < rate <= 120:
            raise ValueError("Invalid patch frame rate")
        seen = set()
        for patch in self.patches:
            if patch.frame_index >= self.frames or patch.frame_index in seen:
                raise ValueError("Patch frames must be unique and within the source")
            if patch.rectangle[2] > self.width or patch.rectangle[3] > self.height:
                raise ValueError("Patch rectangle leaves source geometry")
            seen.add(patch.frame_index)
        return self


class StepObservation(ExecutionContract):
    revision: Literal[1] = 1
    base_sha256: str
    scope_sha256: str
    capability_sha256: str
    evidence_sha256: str
    action: str
    method: str | None = None
    candidate_id: str | None = None

    @property
    def incident_id(self):
        # A new message or turn number cannot reopen the same incident.
        return identity(self.model_dump(exclude={"action", "method", "candidate_id"}))


EXECUTION_CONTRACTS = {
    "execution/profile": ExecutionProfile,
    "execution/patch-manifest": PatchManifest,
    "execution/step-observation": StepObservation,
}
