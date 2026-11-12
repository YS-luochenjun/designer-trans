"""Task-scoped delegation and the read-only Route agent's recovery protocol."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RouteAutonomy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=2400)
    max_route_calls_per_hour: int = Field(default=12, ge=1, le=60)
    cooldown_seconds: int = Field(default=30, ge=15, le=1800)
    diagnose_request_rejections: bool = False
    recover_observation_context: bool = False
    recover_production_route: bool = False


class RouteRecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["replace_work", "reattach_observation", "handoff_work", "probe",
                    "verify_work_contract", "wait", "needs_user"]
    reason: str = Field(min_length=1, max_length=2400)
    evidence: list[str] = Field(min_length=1, max_length=16)
    probe: Literal["receipt_integrity", "schema_contract", "runtime_readiness"] | None = None
    delay_seconds: int = Field(default=60, ge=15, le=1800)
