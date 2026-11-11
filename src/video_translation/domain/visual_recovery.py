"""Explicit authority for one missing-answer visual incident."""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class VisualRecoveryAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approve: bool
    expected_decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=2400)

    @model_validator(mode="after")
    def approved(self):
        if not self.approve:
            raise ValueError("Explicit visual recovery approval is required")
        return self
