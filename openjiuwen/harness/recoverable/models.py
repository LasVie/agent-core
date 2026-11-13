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
    """vcs log position of ``commit_id``; equal positions mean the context did not change."""
    manifest_id: str | None = None
    """Workspace manifest at this checkpoint; ``None`` when the workspace is not snapshotted."""
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
    workspace_restored: bool = False
    """False when the workspace is not snapshotted (sandbox, or no manifest at the target)."""
    files_to_write: list[str] = Field(default_factory=list)
    files_to_delete: list[str] = Field(default_factory=list)
    unrecoverable_files: list[str] = Field(default_factory=list)
    """Changed since the target, but their content there was never stored."""
    build_outputs_to_delete: list[str] = Field(default_factory=list)
    lost_commits: int | None = None
    """Commits on the current git branches that disappear; ``None`` without git."""
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
    workspace_restored: bool = False
    files_written: list[str] = Field(default_factory=list)
    files_deleted: list[str] = Field(default_factory=list)
    build_outputs_deleted: list[str] = Field(default_factory=list)
    unrestored_files: list[str] = Field(default_factory=list)
    """Could not be restored: never stored, or writing them failed."""
    applied_at: float = Field(default_factory=time.time)


class PendingRollback(BaseModel):
    """An applied rollback whose context is written back when the next round starts.

    Between rounds no live session exists to write into, so the context is
    restored, and the rollback checkpoint created, at the next ``BEFORE_INVOKE``.
    The workspace is restored at apply time already. Notes accumulate when a
    second rollback lands before the next round.
    """

    schema_version: int = SCHEMA_VERSION
    session_id: str
    target_id: str
    manifest_id: str | None = None
    """Workspace manifest right after the files were restored."""
    notes: list[str] = Field(default_factory=list)
