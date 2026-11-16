# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Records kept by Recoverable Harness.

Every persisted record carries ``schema_version``; a reader refuses versions it
does not know instead of guessing at their shape.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from openjiuwen.harness.recoverable.effects import EffectCategory

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
    ledger_seq: int = 0
    """Highest side-effect record ``seq`` at this checkpoint; later records happened after it."""
    kind: CheckpointKind = CheckpointKind.STEP
    created_at: float = Field(default_factory=time.time)


class EffectStatus(str, Enum):
    INTENDED = "intended"
    """Recorded before the call; no outcome yet. After a crash this means 'status unknown'."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    """The tool raised or reported failure. Part of the effect may still have happened."""


class LedgerRecord(BaseModel):
    """One tool call that may have changed something outside the workspace."""

    schema_version: int = SCHEMA_VERSION
    record_id: str
    session_id: str
    seq: int
    """Order of registration in the session, starting at 1."""
    tool_call_id: str
    tool_name: str
    args_summary: str = ""
    category: EffectCategory
    status: EffectStatus = EffectStatus.INTENDED
    error: str | None = None
    undo_tool: str | None = None
    undo_args: dict[str, Any] | None = None
    """Arguments of ``undo_tool``; ``None`` when the declared sources were missing."""
    reverses: str | None = None
    """On a record of an undo call: the record it undoes."""
    rollback_id: str | None = None
    """On a record of an undo call: the rollback (plan) that ran it."""
    abandoned: bool = False
    """Set when a rollback discards the step this call belongs to."""
    created_at: float = Field(default_factory=time.time)
    completed_at: float | None = None


class EffectAction(str, Enum):
    """What a rollback does about one recorded effect."""

    UNDO = "undo"
    IRREVERSIBLE = "irreversible"
    CANNOT_UNDO = "cannot_undo"
    """Declared reversible, but the arguments for the undo tool were not in the call."""
    UNKNOWN = "unknown"
    """The call failed or never reported an outcome; nothing is undone."""
    RETAINED = "retained"


class EffectOutcome(str, Enum):
    UNDONE = "undone"
    CANCELLED = "cancelled"
    """A person struck this undo from the plan when confirming."""
    FAILED = "failed"
    PENDING = "pending"
    """Not attempted because an earlier undo failed; ``resume_rollback`` tries it."""


class PlannedEffect(BaseModel):
    record_id: str
    tool_name: str
    args_summary: str = ""
    action: EffectAction
    undo_tool: str | None = None
    undo_args: dict[str, Any] | None = None
    outcome: EffectOutcome | None = None
    detail: str = ""


class RollbackStatus(str, Enum):
    PLANNED = "planned"
    COMPLETED = "completed"
    PARTIAL = "partial"
    """Some undo failed or some file was not restored; see the result."""
    ACCEPTED = "accepted"
    """A partial rollback a person accepted as it is."""


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
    effects: list[PlannedEffect] = Field(default_factory=list)
    """Recorded effects after the target, newest first: the order undos run in."""
    status: RollbackStatus = RollbackStatus.PLANNED
    created_at: float = Field(default_factory=time.time)
    applied_at: float | None = None
    """Set once the plan is applied; a plan is applied at most once."""


class RollbackApproval(BaseModel):
    """A person's confirmation of one plan, bound to its target."""

    plan_id: str
    target_id: str
    cancelled_effect_ids: list[str] = Field(default_factory=list)
    """Records whose undo the person struck from the plan."""


class RollbackResult(BaseModel):
    """Outcome of an applied rollback."""

    plan_id: str
    session_id: str
    target_id: str
    abandoned_ids: list[str]
    status: RollbackStatus = RollbackStatus.COMPLETED
    effects: list[PlannedEffect] = Field(default_factory=list)
    """The plan's effects, each undo with its outcome."""
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
    target_id: str | None
    """``None`` when only notes are pending, such as the outcome of a resumed rollback."""
    manifest_id: str | None = None
    """Workspace manifest right after the files were restored."""
    notes: list[str] = Field(default_factory=list)
