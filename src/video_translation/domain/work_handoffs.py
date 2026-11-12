"""Version-bound upstream evidence, without workflow or permission changes."""
from pydantic import BaseModel, ConfigDict, Field

from .execution import FileRef


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_node_id: str = Field(min_length=1, max_length=128)
    target_node_id: str = Field(min_length=1, max_length=128)
    expected_basis_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(min_length=1, max_length=2000)
    evidence: list[FileRef] = Field(default_factory=list, max_length=12)
