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
    packages_id: str | None = None
    """Package lists of the workspace's dependency environments at this checkpoint."""
    ledger_seq: int = 0
    """Highest side-effect record ``seq`` at this checkpoint; later records happened after it."""
    kind: CheckpointKind = CheckpointKind.STEP
    label: str = ""
    """One line on what the context ends with here, for a person picking a target: the step's tool calls,
    the model's reply, the user's message, or the rollback that made it."""
    created_at: float = Field(default_factory=time.time)


class EffectStatus(str, Enum):
    INTENDED = "intended"
    """Recorded before the call; no outcome yet. After a crash this means 'status unknown'."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    """The tool raised or reported failure. Part of the effect may still have happened."""


class PathBackup(BaseModel):
    """One path outside the workspace that a call declared it writes, before and right after the call.

    Entries are ``Entry.to_list()`` values keyed by POSIX path relative to the
    parent of ``path``, so the path itself is keyed by its own name.
    """

    path: str
    """Absolute path, after ``~`` and a relative path are resolved."""
    before: dict[str, list] | None = None
    """What the path held before the call; ``None`` when it did not exist."""
    error: str | None = None
    """Why the path was not backed up, such as too large or unreadable. It cannot be put back."""
    after: dict[str, list] | None = None
    """What the call left; ``None`` when the call left nothing there."""
    after_taken: bool = False
    """False until ``after`` is recorded. Without it a later change by someone else cannot be told apart."""


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
    detail: str = ""
    """Why the call got its category, such as an outward command declared without effect."""
    external_paths: list[str] = Field(default_factory=list)
    """Paths outside the workspace the call declared it writes."""
    external_backups: list[PathBackup] = Field(default_factory=list)
    """Backups of ``external_paths``, taken before the call; a rollback puts them back."""
    pid: int | None = None
    """Process group of a command started in the background; stopped on rollback."""
    pid_started: str | None = None
    """Start time of ``pid`` as ``ps`` reports it, to tell a reused pid apart."""
    generated_undo: str | None = None
    """Shell command reversing the global packages this call changed, worked out by comparing lists."""
    generated_cannot: list[str] = Field(default_factory=list)
    """Global package changes that cannot be reversed, such as a brew upgrade."""
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
    REVIEW = "review"
    """The call failed but has an undo; it runs only if the person confirms it."""
    RETAINED = "retained"


class EffectOutcome(str, Enum):
    UNDONE = "undone"
    CANCELLED = "cancelled"
    """A person struck this undo from the plan when confirming."""
    FAILED = "failed"
    PENDING = "pending"
    """Not attempted because an earlier undo failed; ``resume_rollback`` tries it."""
    KEPT = "kept"
    """An outside file changed again after the agent's call, so it was left as it is."""


class PlannedEffect(BaseModel):
    record_id: str
    tool_name: str
    args_summary: str = ""
    action: EffectAction
    undo_tool: str | None = None
    undo_args: dict[str, Any] | None = None
    path: str | None = None
    """Set on an outside file: the undo puts this path back from the call's backup, no tool runs."""
    outcome: EffectOutcome | None = None
    detail: str = ""


class PackageChangeInfo(BaseModel):
    """How one dependency environment is brought back to its list at the target."""

    env: str
    """Workspace-relative path, such as ``.venv`` or ``web/node_modules``."""
    remove: list[str] = Field(default_factory=list)
    install: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    """Set when the environment could not be restored."""


class RollbackStatus(str, Enum):
    PLANNED = "planned"
    APPLYING = "applying"
    """Being applied. A crash leaves it here; ``resume_rollback`` finishes it from where it stopped."""
    COMPLETED = "completed"
    PARTIAL = "partial"
    """Some undo failed or some file was not restored; see the result."""
    ACCEPTED = "accepted"
    """A partial rollback a person accepted as it is."""
    INVALIDATED = "invalidated"
    """Declined by a person or superseded; it can no longer be applied."""


class RollbackStage(str, Enum):
    """The last stage of an applying rollback that finished, in order."""

    STARTED = "started"
    PROCESSES = "processes"
    UNDOS = "undos"
    DEPENDENCIES = "dependencies"
    FILES = "files"


class RollbackProgress(BaseModel):
    """How far an applying rollback got, and what its finished stages did, for the note after a crash."""

    stage: RollbackStage = RollbackStage.STARTED
    abandoned_ids: list[str] = Field(default_factory=list)
    """Checkpoints after the target when the rollback started; deleted at the commit."""
    stopped: dict[int, str] = Field(default_factory=dict)
    """Background process to what stopping it did."""
    packages_id: str | None = None
    workspace_restored: bool = False
    files_written: list[str] = Field(default_factory=list)
    files_deleted: list[str] = Field(default_factory=list)
    build_outputs_deleted: list[str] = Field(default_factory=list)
    unrestored_files: list[str] = Field(default_factory=list)
    manifest_id: str | None = None
    """Workspace manifest right after the files were restored."""


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
    processes_to_stop: list[int] = Field(default_factory=list)
    """Background processes the agent started after the target."""
    package_changes: list[PackageChangeInfo] = Field(default_factory=list)
    """Dependency environments whose package list differs from the target's."""
    status: RollbackStatus = RollbackStatus.PLANNED
    signal_id: str | None = None
    """The rollback signal this plan answers; ``None`` for a plan asked for directly."""
    progress: RollbackProgress | None = None
    """Set when applying starts and after each stage; the rollback progress record."""
    created_at: float = Field(default_factory=time.time)
    applied_at: float | None = None
    """Set once the plan is applied; a plan is applied at most once."""


class RollbackApproval(BaseModel):
    """A person's confirmation of one plan, bound to its target."""

    plan_id: str
    target_id: str
    cancelled_effect_ids: list[str] = Field(default_factory=list)
    """Records whose undo the person struck from the plan."""
    confirmed_effect_ids: list[str] = Field(default_factory=list)
    """``review`` records whose undo the person chose to run."""


class RollbackResult(BaseModel):
    """Outcome of an applied rollback."""

    plan_id: str
    session_id: str
    target_id: str
    abandoned_ids: list[str]
    status: RollbackStatus = RollbackStatus.COMPLETED
    effects: list[PlannedEffect] = Field(default_factory=list)
    """The plan's effects, each undo with its outcome."""
    processes_stopped: list[int] = Field(default_factory=list)
    package_changes: list[PackageChangeInfo] = Field(default_factory=list)
    """Dependency environments restored, with ``error`` set where that failed."""
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
    packages_id: str | None = None
    """Package lists right after the dependencies were restored."""
    notes: list[str] = Field(default_factory=list)
    plan_ids: list[str] = Field(default_factory=list)
    """Rollbacks whose note is in ``notes``, so a resumed commit does not add it twice."""


class RollbackSignal(BaseModel):
    """A request to roll a session back, from a person or an automatic monitor such as JEV."""

    signal_id: str
    """Chosen by the sender. The same signal sent twice gets the same ticket."""
    session_id: str
    target_id: str
    initiator: Initiator = Initiator.HUMAN
    source: str = ""
    """Who sent it, such as ``JEV``; only shown to people and the agent."""
    reason: str = ""


class SignalStatus(str, Enum):
    WAITING = "waiting"
    """Received while the agent runs; the round ends at the next step boundary and a plan follows."""
    AWAITING_APPROVAL = "awaiting_approval"
    """The plan is ready; a person applies it with ``apply_rollback`` or declines it."""
    REJECTED = "rejected"
    """Refused on arrival: bad target, another rollback in progress, or over the automatic limit."""
    INVALIDATED = "invalidated"
    """Declined by a person, or superseded by a person's signal or another applied rollback."""
    APPLIED = "applied"


class RollbackTicket(BaseModel):
    """Where one rollback signal stands. ``detail`` says why it was rejected or invalidated."""

    schema_version: int = SCHEMA_VERSION
    signal: RollbackSignal
    status: SignalStatus
    plan_id: str | None = None
    detail: str = ""
    updated_at: float = Field(default_factory=time.time)


class RollbackCounts(BaseModel):
    """Automatic rollbacks of one session, kept apart from the history so a rollback cannot reset them."""

    schema_version: int = SCHEMA_VERSION
    session_id: str
    round: int = 0
    """Rounds started in this session."""
    auto_this_round: int = 0
    """Automatic signals accepted since the round began. A round that starts by writing back a
    rollback continues the count: it carries on the task the rollback interrupted."""
    auto_by_target: dict[str, int] = Field(default_factory=dict)


class EventKind(str, Enum):
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_FAILED = "checkpoint_failed"
    ROLLBACK_RECEIVED = "rollback_received"
    ROLLBACK_REJECTED = "rollback_rejected"
    ROLLBACK_AWAITING_APPROVAL = "rollback_awaiting_approval"
    ROLLBACK_INVALIDATED = "rollback_invalidated"
    ROLLBACK_APPLIED = "rollback_applied"
    ROLLBACK_PARTIAL = "rollback_partial"
    STORAGE_LOW = "storage_low"
    """Storage passed a limit. Nothing is deleted: the user frees space, raises the limit or prunes checkpoints."""
    CHECKPOINTS_DROPPED = "checkpoints_dropped"
    """Old checkpoints were deleted at the user's request; they are no longer rollback targets."""


class RecoverableEvent(BaseModel):
    """A notice for other layers. The facts stay in the checkpoint store and the rollback plans."""

    kind: EventKind
    session_id: str
    round: int | None = None
    checkpoint_id: str | None = None
    seq: int | None = None
    """Position of the checkpoint on the session's line."""
    signal_id: str | None = None
    plan_id: str | None = None
    detail: str = ""
    created_at: float = Field(default_factory=time.time)


class StorageStatus(BaseModel):
    """How much room the workspace's rollback data takes, and whether a limit is passed."""

    stored_bytes: int
    """File content stored for the workspace. Clones share disk blocks, so the disk may hold less."""
    storage_cap: int | None
    disk_free: int
    disk_total: int
    min_free_ratio: float
    over_limit: str | None = None
    """Which limit is passed, or ``None``."""


class PruneReport(BaseModel):
    """What deleting old checkpoints frees, or would free with ``dry_run``."""

    keep_latest: int
    dry_run: bool
    checkpoints: dict[str, int] = Field(default_factory=dict)
    """Checkpoints deleted per session."""
    freed_bytes: int = 0
