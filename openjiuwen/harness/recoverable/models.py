# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Records kept by Recoverable Harness.

Every persisted record carries ``schema_version``; a reader refuses versions it
does not know instead of guessing at their shape.
"""

from __future__ import annotations

import time
from enum import Enum

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class CheckpointKind(str, Enum):
    """Why a checkpoint was taken."""

    STEP = "step"
    """A step boundary: round start, step end or round end."""
    ROLLBACK = "rollback"
    """The point right after a rollback; its context ends with the rollback note."""


class Initiator(str, Enum):
    """Who asked for a rollback. Authorization rules look only at this."""

    HUMAN = "human"
    AUTO = "auto"


class Checkpoint(BaseModel):
    """A rollback target: positions in the logs at one step boundary.

    History is a single line. ``parent_id`` is the checkpoint before this one;
    a rollback checkpoint's parent is the rollback target.
    """

    schema_version: int = SCHEMA_VERSION
    checkpoint_id: str
    session_id: str
    seq: int
    """Position on the current line, starting at 0. Reused after a rollback."""
    parent_id: str | None = None
    commit_id: str
    """vcs commit holding the context and state of this checkpoint."""
    event_id: int
    """vcs log position of ``commit_id``; equal positions mean nothing changed."""
    kind: CheckpointKind = CheckpointKind.STEP
    created_at: float = Field(default_factory=time.time)


class RollbackPlan(BaseModel):
    """What a rollback will do, shown to a person before it runs."""

    schema_version: int = SCHEMA_VERSION
    plan_id: str
    session_id: str
    target_id: str
    head_id: str
    abandoned_ids: list[str]
    """Checkpoints after the target, newest first. They are deleted on apply."""
    initiator: Initiator = Initiator.HUMAN
    source: str = ""
    reason: str = ""
    created_at: float = Field(default_factory=time.time)
    applied_at: float | None = None
    """Set once the plan is applied; a plan is applied at most once."""


class RollbackApproval(BaseModel):
    """A person's confirmation of one plan, bound to its target."""

    plan_id: str
    target_id: str


class RollbackResult(BaseModel):
    """Outcome of an applied rollback."""

    plan_id: str
    session_id: str
    target_id: str
    abandoned_ids: list[str]
    applied_at: float = Field(default_factory=time.time)


class PendingRollback(BaseModel):
    """An applied rollback whose context is written back when the next round starts.

    Between rounds no live session exists to write into, so the context is
    restored, and the rollback checkpoint created, at the next ``BEFORE_INVOKE``.
    Notes accumulate when a second rollback lands before that.
    """

    schema_version: int = SCHEMA_VERSION
    session_id: str
    target_id: str
    notes: list[str] = Field(default_factory=list)
