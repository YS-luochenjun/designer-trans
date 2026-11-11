"""Explicit admission limits; account identity and source trust are never inferred."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from .execution import ExecutionContract, ExecutionMode


class IntakePolicy(ExecutionContract):
    revision: Literal[1] = 1
    identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: str = Field(min_length=1, max_length=128)
    delivery_types: list[str] = Field(min_length=1, max_length=20)
    locales: list[str] = Field(min_length=1, max_length=12)
    trusted_hosts: list[str] = Field(min_length=1, max_length=20)
    trusted_origin_fields: list[Literal["description", "refText"]] = Field(min_length=1)
    department_ids: list[str] = Field(min_length=1, max_length=30)
    max_queue: int = Field(default=10, ge=1, le=100)
    max_active: int = Field(default=1, ge=1, le=10)
    max_scan: int = Field(default=1000, ge=1, le=10000)
    daily_calls: int = Field(ge=0)
    daily_images: int = Field(ge=0)
    daily_kie_credits: int = Field(ge=0)
    task_calls: int = Field(ge=0, le=1000)
    task_images: int = Field(ge=0, le=100)
    task_kie_credits: int = Field(ge=0, le=10000)
    max_visual_steps: int = Field(default=30, ge=4, le=1000)
    execution_profile: ExecutionMode = "efficient_v1"
    notes: str = Field(default="", max_length=2000)

    @field_validator("trusted_hosts")
    @classmethod
    def exact_hosts(cls, values):
        for host in values:
            parsed = urlsplit("https://" + host)
            if (not parsed.hostname or parsed.hostname != host or parsed.netloc != host
                    or "." not in host or host.startswith(".") or "*" in host):
                raise ValueError("Trust requires exact lowercase HTTPS hostnames")
        return sorted(set(values))

    @field_validator("locales")
    @classmethod
    def valid_locales(cls, values):
        import re
        if len(values) != len(set(values)) or any(
                not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value) for value in values):
            raise ValueError("Policy locales must be unique canonical language tags")
        return values
