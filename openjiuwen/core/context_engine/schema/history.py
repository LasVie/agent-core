# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in, lossless Session history contracts."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionHistoryConfig(BaseModel):
    """Host-owned storage root; Session IDs are mapped to safe directory names."""

    enabled: bool = False
    root_dir: str | None = None
    output_reserve_tokens: int = Field(default=4096, ge=0)
    safety_margin_tokens: int = Field(default=256, ge=0)

    @model_validator(mode="after")
    def validate_enabled(self) -> "SessionHistoryConfig":
        """Require storage only when the feature is explicitly enabled."""
        if self.enabled and not self.root_dir:
            raise ValueError("enabled Session history requires root_dir")
        return self


class ArchiveRecord(BaseModel):
    """One original message. Unknown legacy occurrence times remain None."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    execution_id: str
    step_id: str
    seq: int = Field(ge=0)
    message_id: str
    occurred_at: datetime | None
    archived_at: datetime | None = None
    message: dict[str, Any]


class ArchiveRef(BaseModel):
    """A file reference, without a generated summary or truncated preview."""

    model_config = ConfigDict(frozen=True)

    archive_id: str
    path: str
    message_ids: tuple[str, ...]
    first_occurred_at: datetime | None
    last_occurred_at: datetime | None


class ExecutionRecord(BaseModel):
    """The result of saving one outer execution's original trajectory."""

    model_config = ConfigDict(frozen=True)

    execution_id: str
    status: Literal["completed", "interrupted", "failed"]
    trajectory: ArchiveRef
