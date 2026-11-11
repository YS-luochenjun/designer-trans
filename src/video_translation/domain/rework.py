"""Versioned scope and new allowance for an isolated directed-rework branch."""
from pydantic import Field

from .execution import ExecutionContract


class ReworkRequest(ExecutionContract):
    node_id: str = Field(min_length=1, max_length=128)
    locale: str = Field(min_length=2, max_length=35)
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    max_paid_calls: int = Field(ge=0, le=1000)
    max_image_calls: int = Field(ge=0, le=100)
    max_kie_credits: int = Field(ge=0, le=10000)
