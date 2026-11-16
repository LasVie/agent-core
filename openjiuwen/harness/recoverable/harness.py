# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RecoverableHarness: step checkpoints and confirmed rollback for one DeepAgent.

A checkpoint is a set of positions taken at a step boundary: the vcs commit that
holds the context and DeepAgent state, and the manifest of the workspace. A
rollback is planned, confirmed by a person, then applied between rounds:

1. ``apply_rollback`` makes the workspace match the target's manifest, deletes
   the checkpoints after the target, moves HEAD to it and leaves a pending
   rollback.
2. The next round's ``BEFORE_INVOKE`` writes the target context back into the
   live session through vcs ``rewind`` (which also drops the later log), appends
   the rollback note and commits the rollback checkpoint.

The same ``BEFORE_INVOKE`` hook restores the session after a process restart:
when the host reopens a session whose live context is empty, HEAD is written back
before anything new is recorded.

The workspace snapshot of a new session (its first manifest) is taken in the
background; every tool call waits for it, model calls do not.

Tool calls that may change something outside the workspace are written to the
side-effect ledger before they run (a call whose record cannot be written does
not run) and completed with their outcome after. A rollback undoes the reversible
ones, newest first, before it restores the workspace; it stops at the first undo
that fails and reports the rollback as partial.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import create_async_engine

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage
from openjiuwen.core.foundation.llm.schema.message import OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA
from openjiuwen.core.foundation.store.base_kv_store import BaseKVStore
from openjiuwen.core.foundation.store.kv.db_based_kv_store import DbBasedKVStore
from openjiuwen.core.session.vcs import VersioningConfig, VersioningManager, for_session
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.sys_operation.execution_environment import ExecutionEnvironment, execution_environment_scope
from openjiuwen.harness.recoverable.effects import (
    DeclarationTable,
    EffectCategory,
    EffectDeclaration,
    build_undo_args,
    is_failure,
    parse_args,
)
from openjiuwen.harness.recoverable.manifest import Manifest, ManifestRecord
from openjiuwen.harness.recoverable.models import (
    Checkpoint,
    CheckpointKind,
    EffectAction,
    EffectOutcome,
    EffectStatus,
    Initiator,
    LedgerRecord,
    PendingRollback,
    PlannedEffect,
    RollbackApproval,
    RollbackPlan,
    RollbackResult,
    RollbackStatus,
)
from openjiuwen.harness.recoverable.objects import ObjectStore
from openjiuwen.harness.recoverable.rail import CheckpointRail
from openjiuwen.harness.recoverable.store import CheckpointStore
from openjiuwen.harness.recoverable.workspace import (
    DEFAULT_BUILD_OUTPUT_DIRS,
    DEFAULT_DEPENDENCY_DIRS,
    RestoreReport,
    WorkspaceSnapshot,
)

if TYPE_CHECKING:
    from openjiuwen.core.context_engine import ContextEngine
    from openjiuwen.core.context_engine.base import ModelContext
    from openjiuwen.core.session.agent import Session
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
    from openjiuwen.harness.deep_agent import DeepAgent

DEFAULT_STORAGE_DIR = Path.home() / ".openjiuwen" / "recoverable"
DEFAULT_FILE_SIZE_CAP = 1 << 30
"""Largest file stored when the file system cannot clone (1 GB)."""
ROLLBACK_NOTE_SOURCE_KIND = "rollback_note"
_DB_FILENAME = "recoverable.db"
_LIST_LIMIT = 10
_SUMMARY_LIMIT = 500
_RECORD_KEY = "_recoverable_ledger_record"
_UNRECORDED = "the side effect of this call could not be recorded, so the call was not run"


def _at_step_boundary(context: "ModelContext") -> bool:
    """True when the latest assistant tool calls all have results."""
    messages = context.get_messages()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AssistantMessage) and message.tool_calls:
            answered = {m.tool_call_id for m in messages[index + 1:] if isinstance(m, ToolMessage)}
            return all(call.id in answered for call in message.tool_calls)
    return True


def _live_is_empty(session: "Session", context_engine: "ContextEngine") -> bool:
    """True when neither the context pool nor the session state holds a message."""
    context = context_engine.get_context(session_id=session.get_session_id())
    if context is not None and context.get_messages():
        return False
    states = session.get_state("context")
    if not isinstance(states, dict):
        return True
    return not any(isinstance(state, dict) and state.get("messages") for state in states.values())


def _listed(paths: list[str]) -> str:
    shown = ", ".join(paths[:_LIST_LIMIT])
    return shown + (f" and {len(paths) - _LIST_LIMIT} more" if len(paths) > _LIST_LIMIT else "")


def _summary(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:_SUMMARY_LIMIT]


def _label(effect: PlannedEffect) -> str:
    return f"{effect.tool_name}({effect.args_summary[:80]})"


def _effect_lines(effects: list[PlannedEffect]) -> list[str]:
    groups: list[tuple[str, list[PlannedEffect]]] = [
        ("Undone outside the workspace", [e for e in effects if e.outcome == EffectOutcome.UNDONE]),
        ("Left in place at the user's request", [e for e in effects if e.outcome == EffectOutcome.CANCELLED]),
        ("Undo FAILED or was not attempted; still in effect",
         [e for e in effects if e.outcome in (EffectOutcome.FAILED, EffectOutcome.PENDING)]),
        ("Could NOT be undone and still in effect", [e for e in effects if e.action == EffectAction.IRREVERSIBLE]),
        ("Could NOT be undone (the information needed was missing)",
         [e for e in effects if e.action == EffectAction.CANNOT_UNDO]),
        ("Outcome unknown, not undone", [e for e in effects if e.action == EffectAction.UNKNOWN]),
        ("Kept on purpose", [e for e in effects if e.action == EffectAction.RETAINED]),
    ]
    lines = [f"{title}: {_listed([_label(e) for e in group])}." for title, group in groups if group]
    if not effects:
        lines.append("No effects outside the workspace were recorded after that checkpoint.")
    lines.append("Effects the harness did not record, such as those inside subagents, were not handled.")
    return lines


def _rollback_note(
    plan: RollbackPlan, target: Checkpoint, abandoned: int, report: RestoreReport | None,
    effects: list[PlannedEffect],
) -> str:
    who = "The user" if plan.initiator == Initiator.HUMAN else (plan.source or "An automatic monitor")
    lines = [
        f"[ROLLBACK] {who} rolled this conversation back to checkpoint #{target.seq}.",
        f"The {abandoned} later checkpoint(s) were discarded, with their model replies, tool calls and tool results.",
    ]
    if report is None:
        lines.append("Workspace files were NOT rolled back; check them before relying on them.")
    else:
        lines.append(
            f"Workspace files are back as they were at that checkpoint "
            f"({len(report.written)} written back, {len(report.deleted)} deleted)."
        )
        if report.build_outputs_deleted:
            lines.append(f"Build output directories were deleted; rebuild before using them: "
                         f"{_listed(report.build_outputs_deleted)}.")
        unrestored = sorted({*report.unrecoverable, *report.failed})
        if unrestored:
            lines.append(f"These files could NOT be restored and may still hold later content: {_listed(unrestored)}.")
        lines.append("Dependency directories (such as node_modules or .venv) and memory were NOT rolled back.")
    lines.extend(_effect_lines(effects))
    if plan.reason:
        lines.append(f"Reason: {plan.reason}")
    return "\n".join(lines)


@dataclass
class _Files:
    """Workspace snapshot state of one session."""

    manifest: Manifest | None = None
    """Latest scan. Its stat fields are the cache the next scan trusts."""
    manifest_id: str | None = None
    depth: int = 0
    baseline: asyncio.Task | None = None


class RecoverableHarness:
    """Checkpoints every step of a DeepAgent and rolls back to one on confirmation.

    Register :meth:`rail` on the agent. All records persist in ``kv_store``, by
    default a SQLite file under ``storage_dir``, so rollback still works after the
    process restarts. File content is kept under ``storage_dir/objects``.

    Args:
        kv_store: Store for checkpoints, manifests and vcs logs. Defaults to SQLite
            at ``storage_dir/recoverable.db``.
        storage_dir: Directory for Recoverable Harness data. It must lie outside
            the agent workspace, ideally on the same volume so files can be cloned.
            Defaults to ``~/.openjiuwen/recoverable``.
        dependency_dirs: Directory names left out of the snapshot at any depth.
        build_output_dirs: Directory names that, when git ignores them, are left
            out of the snapshot and deleted on rollback.
        file_size_cap: Largest file stored when the file system cannot clone;
            ``None`` for no cap. ``.git`` is never capped.
        use_clones: Try file clones before copying.
        declarations: Side-effect declarations of the host's tools, by tool name.
            They extend and override the built-in table; a tool in neither is
            irreversible.
    """

    def __init__(
        self,
        *,
        kv_store: BaseKVStore | None = None,
        storage_dir: str | Path | None = None,
        dependency_dirs: frozenset[str] = DEFAULT_DEPENDENCY_DIRS,
        build_output_dirs: frozenset[str] = DEFAULT_BUILD_OUTPUT_DIRS,
        file_size_cap: int | None = DEFAULT_FILE_SIZE_CAP,
        use_clones: bool = True,
        declarations: dict[str, EffectDeclaration] | None = None,
    ):
        self._storage_dir = Path(storage_dir).expanduser().resolve() if storage_dir else DEFAULT_STORAGE_DIR
        self._kv_store = kv_store
        self._store: CheckpointStore | None = None
        self._dependency_dirs = frozenset(dependency_dirs)
        self._build_output_dirs = frozenset(build_output_dirs)
        self._file_size_cap = file_size_cap
        self._use_clones = use_clones
        self._declarations = DeclarationTable(declarations)
        self._workspace: WorkspaceSnapshot | None = None
        self._workspace_key = ""
        self._rail = CheckpointRail(self)
        self._managers: dict[str, tuple[weakref.ref, VersioningManager]] = {}
        self._files: dict[str, _Files] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ledger_locks: dict[str, asyncio.Lock] = {}
        self._running: set[str] = set()

    @property
    def storage_dir(self) -> Path:
        return self._storage_dir

    def rail(self) -> CheckpointRail:
        """The rail to register on the DeepAgent this harness serves."""
        return self._rail

    def check_workspace(self, workspace_root: str | Path) -> None:
        """Refuse storage inside the workspace, where the agent's tools could rewrite it."""
        root = Path(workspace_root).expanduser().resolve()
        if self._storage_dir == root or root in self._storage_dir.parents:
            raise build_error(
                StatusCode.DEEPAGENT_CONFIG_PARAM_ERROR,
                error_msg=(
                    f"recoverable storage {self._storage_dir} is inside the workspace {root}; "
                    "pass a storage_dir outside the workspace"
                ),
            )

    def bind_workspace(
        self, workspace_root: str | Path, *, sandboxed: bool, kept_dirs: frozenset[str] = frozenset(),
    ) -> None:
        """Snapshot ``workspace_root``; a sandboxed workspace is not on this machine and is skipped.

        ``kept_dirs`` are workspace-relative directories that rollback leaves alone,
        such as the agent's memory.
        """
        self.check_workspace(workspace_root)
        if sandboxed:
            self._workspace = None
            logger.warning("recoverable: the workspace is in a sandbox; files will not be rolled back")
            return
        root = Path(workspace_root).expanduser().resolve()
        self._workspace_key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        objects = ObjectStore(
            self._storage_dir / "objects" / self._workspace_key,
            size_cap=self._file_size_cap,
            use_clones=self._use_clones,
        )
        self._workspace = WorkspaceSnapshot(
            root, objects, dependency_dirs=self._dependency_dirs, build_output_dirs=self._build_output_dirs,
            kept_dirs=kept_dirs,
        )

    def _checkpoints(self) -> CheckpointStore:
        if self._store is None:
            if self._kv_store is None:
                self._storage_dir.mkdir(parents=True, exist_ok=True)
                engine = create_async_engine(f"sqlite+aiosqlite:///{self._storage_dir / _DB_FILENAME}")
                self._kv_store = DbBasedKVStore(engine)
            self._store = CheckpointStore(self._kv_store)
        return self._store

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _ledger_lock(self, session_id: str) -> asyncio.Lock:
        return self._ledger_locks.setdefault(session_id, asyncio.Lock())

    def _ensure_idle(self, session_id: str) -> None:
        if session_id in self._running:
            raise build_error(
                StatusCode.DEEPAGENT_RUNTIME_ERROR,
                error_msg=f"session {session_id} is running a round; roll back after it ends",
            )

    def _manager_for(self, session: "Session", context_engine: "ContextEngine") -> VersioningManager:
        """A vcs manager bound to this live session object, rebuilt when the object changes."""
        session_id = session.get_session_id()
        cached = self._managers.get(session_id)
        if cached is not None and cached[0]() is session:
            return cached[1]
        self._checkpoints()
        manager = for_session(session, context_engine, config=VersioningConfig(backend="kv"), kv_store=self._kv_store)
        self._managers[session_id] = (weakref.ref(session), manager)
        return manager

    # ------------------------------------------------------------------ queries

    async def history(self, session_id: str, limit: int | None = None) -> list[Checkpoint]:
        """Checkpoints of ``session_id`` from HEAD back, newest first."""
        return await self._checkpoints().chain(session_id, limit)

    async def uncovered_files(self, session_id: str) -> list[str]:
        """Workspace files over the size cap: present, but a rollback cannot restore them."""
        files = await self._files_ready(session_id)
        return files.manifest.skipped() if files is not None and files.manifest is not None else []

    # ----------------------------------------------------------------- rollback

    async def plan_rollback(
        self,
        session_id: str,
        target_id: str,
        *,
        initiator: Initiator = Initiator.HUMAN,
        source: str = "",
        reason: str = "",
    ) -> RollbackPlan:
        """Work out what rolling ``session_id`` back to ``target_id`` would change.

        The plan is stored and must be confirmed with :meth:`apply_rollback`.
        Rolling back to HEAD itself discards only what happened after it, such as
        a step that stopped on an interrupt or files changed since.
        """
        store = self._checkpoints()
        chain = await store.chain(session_id)
        ids = [checkpoint.checkpoint_id for checkpoint in chain]
        if target_id not in ids:
            raise build_error(
                StatusCode.DEEPAGENT_INPUT_PARAM_ERROR,
                error_msg=f"checkpoint {target_id} is not on the history of session {session_id}",
            )
        target = chain[ids.index(target_id)]
        plan = RollbackPlan(
            plan_id=uuid.uuid4().hex,
            session_id=session_id,
            target_id=target_id,
            head_id=ids[0],
            abandoned_ids=ids[:ids.index(target_id)],
            initiator=initiator,
            source=source,
            reason=reason,
            effects=await self._planned_effects(session_id, target),
        )
        loaded = await self._load_manifest(session_id, target.manifest_id)
        files = await self._files_ready(session_id)
        if self._workspace is not None and loaded is not None:
            target_manifest = loaded[0]
            current = await asyncio.to_thread(self._workspace.scan, files.manifest if files else None)
            diff = self._workspace.diff(current, target_manifest)
            lost = await asyncio.to_thread(self._workspace.lost_commits, target_manifest)
            plan = plan.model_copy(update={
                "workspace_restored": True,
                "files_to_write": diff.write,
                "files_to_delete": diff.delete,
                "unrecoverable_files": diff.unrecoverable,
                "build_outputs_to_delete": diff.build_outputs,
                "lost_commits": lost,
            })
        await store.put_plan(plan)
        return plan

    async def apply_rollback(self, approval: RollbackApproval) -> RollbackResult:
        """Apply a confirmed plan between rounds; the context follows at the next round.

        Order: undo the recorded effects newest first, then restore the workspace,
        then discard the later checkpoints. The first undo that fails stops the
        remaining undos; the workspace is still restored and the rollback ends as
        ``partial``.

        Raises:
            BaseError: the approval does not match a stored, unapplied plan; the
                target left the history; or the session is running a round.
        """
        store = self._checkpoints()
        plan = await store.get_plan(approval.plan_id)
        if plan is None or plan.target_id != approval.target_id or plan.applied_at is not None:
            raise build_error(
                StatusCode.DEEPAGENT_INPUT_PARAM_ERROR,
                error_msg=f"approval does not match an unapplied rollback plan: {approval.plan_id}",
            )
        session_id = plan.session_id
        async with self._lock(session_id):
            self._ensure_idle(session_id)
            chain = await store.chain(session_id)
            ids = [checkpoint.checkpoint_id for checkpoint in chain]
            if plan.target_id not in ids:
                raise build_error(
                    StatusCode.DEEPAGENT_INPUT_PARAM_ERROR,
                    error_msg=f"checkpoint {plan.target_id} is no longer on the history of session {session_id}",
                )
            index = ids.index(plan.target_id)
            target, abandoned = chain[index], ids[:index]
            # Recompute: effects recorded after the plan was made are undone too.
            effects = await self._planned_effects(session_id, target)
            cancelled = set(approval.cancelled_effect_ids)
            effects = [
                e.model_copy(update={"outcome": EffectOutcome.CANCELLED})
                if e.action == EffectAction.UNDO and e.record_id in cancelled else e
                for e in effects
            ]
            effects = await self._run_undos(session_id, plan.plan_id, effects)
            report, manifest_id = await self._restore_workspace(session_id, target)
            await self._abandon_records(session_id, target)
            pending = await store.get_pending(session_id)
            notes = [*pending.notes] if pending is not None else []
            notes.append(_rollback_note(plan, target, len(abandoned), report, effects))
            await store.put_pending(PendingRollback(
                session_id=session_id, target_id=target.checkpoint_id, manifest_id=manifest_id, notes=notes,
            ))
            await store.set_head(session_id, target.checkpoint_id)
            await store.delete(session_id, abandoned)
            kept_manifests = {c.manifest_id for c in chain[index:]} | {manifest_id}
            await store.delete_manifests(
                session_id, sorted({c.manifest_id for c in chain[:index]} - kept_manifests - {None}),
            )
            unrestored = sorted({*report.unrecoverable, *report.failed}) if report else []
            plan = plan.model_copy(update={
                "applied_at": time.time(), "effects": effects, "status": self._status(effects, unrestored),
            })
            await store.put_plan(plan)
            self._managers.pop(session_id, None)
        logger.info(
            "rollback applied: session=%s target=#%s discarded=%s status=%s",
            session_id, target.seq, len(abandoned), plan.status.value,
        )
        return RollbackResult(
            plan_id=plan.plan_id,
            session_id=session_id,
            target_id=target.checkpoint_id,
            abandoned_ids=abandoned,
            status=plan.status,
            effects=effects,
            workspace_restored=report is not None,
            files_written=report.written if report else [],
            files_deleted=report.deleted if report else [],
            build_outputs_deleted=report.build_outputs_deleted if report else [],
            unrestored_files=unrestored,
        )

    async def resume_rollback(self, plan_id: str) -> RollbackResult:
        """Retry the undos a partial rollback did not finish, then restore the workspace again.

        The undos may have changed files, so the workspace is restored once more
        afterwards. The agent learns the outcome from a note at the next round.
        """
        store = self._checkpoints()
        plan = await self._partial_plan(plan_id)
        session_id = plan.session_id
        async with self._lock(session_id):
            self._ensure_idle(session_id)
            target = await store.get(session_id, plan.target_id)
            if target is None:
                raise build_error(
                    StatusCode.DEEPAGENT_INPUT_PARAM_ERROR,
                    error_msg=f"checkpoint {plan.target_id} of rollback {plan_id} no longer exists",
                )
            effects = await self._run_undos(session_id, plan.plan_id, plan.effects)
            report, _ = await self._restore_workspace(session_id, target)
            unrestored = sorted({*report.unrecoverable, *report.failed}) if report else []
            plan = plan.model_copy(update={"effects": effects, "status": self._status(effects, unrestored)})
            await store.put_plan(plan)
            await self._add_note(session_id, "[ROLLBACK] A partial rollback was retried.\n"
                                 + "\n".join(_effect_lines(effects)))
        return RollbackResult(
            plan_id=plan.plan_id,
            session_id=session_id,
            target_id=plan.target_id,
            abandoned_ids=plan.abandoned_ids,
            status=plan.status,
            effects=effects,
            workspace_restored=report is not None,
            files_written=report.written if report else [],
            files_deleted=report.deleted if report else [],
            build_outputs_deleted=report.build_outputs_deleted if report else [],
            unrestored_files=unrestored,
        )

    async def accept_partial(self, plan_id: str) -> RollbackPlan:
        """Accept a partial rollback as it is; what was not undone stays in effect."""
        plan = (await self._partial_plan(plan_id)).model_copy(update={"status": RollbackStatus.ACCEPTED})
        await self._checkpoints().put_plan(plan)
        return plan

    # ------------------------------------------------------- called by the rail

    async def begin_round(self, session: "Session", deep_agent: "DeepAgent") -> None:
        """Mark the session running, write back a pending rollback or a lost session, start the baseline."""
        session_id = session.get_session_id()
        self._running.add(session_id)
        try:
            async with self._lock(session_id):
                await self._restore(session, deep_agent)
                await self._prepare_files(session_id)
        except Exception:
            self._running.discard(session_id)
            raise

    async def end_round(self, session: "Session") -> None:
        """Mark the session idle and keep the latest scan for the workspace's next session."""
        session_id = session.get_session_id()
        try:
            files = self._files.get(session_id)
            if self._workspace is not None and files is not None and files.manifest is not None:
                record = ManifestRecord.build("scan", files.manifest, None)
                await self._checkpoints().put_workspace_scan(self._workspace_key, record)
        finally:
            self._running.discard(session_id)

    async def before_tool(self, session: "Session", ctx: "AgentCallbackContext") -> None:
        """Hold a tool call until the first workspace snapshot exists, then record its intent.

        A call that may change something outside the workspace gets a ledger record
        before it runs. If the record cannot be written, the call is skipped with
        an error result: an effect nobody recorded could never be rolled back.
        """
        files = self._files.get(session.get_session_id())
        if files is not None and files.baseline is not None:
            await files.baseline
        inputs = ctx.inputs
        call_id = str(getattr(getattr(inputs, "tool_call", None), "id", "") or "")
        tool_name = str(getattr(inputs, "tool_name", "") or "")
        skips = ctx.extra.get("_skip_tool_calls")
        if ctx.extra.get("_skip_tool") or (isinstance(skips, dict) and skips.get(call_id)):
            return  # An earlier rail already decided this call does not run.
        declaration = self._declarations.get(tool_name)
        if declaration.category == EffectCategory.NONE:
            return
        session_id = session.get_session_id()
        try:
            store = self._checkpoints()
            async with self._ledger_lock(session_id):
                record = LedgerRecord(
                    record_id=uuid.uuid4().hex,
                    session_id=session_id,
                    seq=await store.next_ledger_seq(session_id),
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    args_summary=_summary(parse_args(getattr(inputs, "tool_args", None))),
                    category=declaration.category,
                )
                await store.put_record(record)
        except Exception:  # Fail closed: the call must not run unrecorded.
            logger.exception("recoverable: cannot record the intent of %s; the call is skipped", tool_name)
            inputs.tool_result = {"success": False, "error": _UNRECORDED}
            inputs.tool_msg = ToolMessage(content=f"Error: {_UNRECORDED}.", tool_call_id=call_id)
            ctx.extra.setdefault("_skip_tool_calls", {})[call_id] = True
            return
        ctx.extra[_RECORD_KEY] = record

    async def after_tool(self, ctx: "AgentCallbackContext", exception: BaseException | None) -> None:
        """Complete the call's ledger record with its outcome and, if reversible, its undo."""
        record: LedgerRecord | None = ctx.extra.pop(_RECORD_KEY, None)
        if record is None:
            return
        if isinstance(exception, ToolInterruptException):
            # The call paused before running; its record stays as intended (status unknown).
            return
        inputs = ctx.inputs
        result = getattr(inputs, "tool_result", None)
        update: dict = {"completed_at": time.time()}
        if exception is not None or is_failure(result):
            update.update(status=EffectStatus.FAILED, error=_summary(str(exception) if exception else result)[:200])
        else:
            update["status"] = EffectStatus.SUCCEEDED
            declaration = self._declarations.get(record.tool_name)
            if declaration.category == EffectCategory.REVERSIBLE:
                args = parse_args(getattr(inputs, "tool_args", None))
                update.update(undo_tool=declaration.undo_tool,
                              undo_args=build_undo_args(declaration, args, result))
        try:
            async with self._ledger_lock(record.session_id):
                await self._checkpoints().put_record(record.model_copy(update=update))
        except Exception:  # The intent stays: on rollback this call reads as "status unknown".
            logger.exception("recoverable: cannot record the outcome of %s", record.tool_name)

    async def take_checkpoint(self, session: "Session", deep_agent: "DeepAgent") -> Checkpoint | None:
        """Record a checkpoint when at a step boundary and something changed; return HEAD."""
        context_engine = deep_agent.react_agent.context_engine
        session_id = session.get_session_id()
        context = context_engine.get_context(session_id=session_id)
        if context is None or not _at_step_boundary(context):
            return None
        async with self._lock(session_id):
            deep_agent.save_state(session)
            manager = self._manager_for(session, context_engine)
            store = self._checkpoints()
            await manager.append()
            manifest_id = await self._snapshot(session_id)
            ledger_seq = await store.ledger_seq(session_id)
            head = await store.head(session_id)
            event_id = manager.current_head().event_id
            if head is not None and (head.event_id, head.manifest_id, head.ledger_seq) == (
                event_id, manifest_id, ledger_seq,
            ):
                return head
            if head is None and event_id == 0:
                return None
            return await self._commit(store, manager, session_id, head, CheckpointKind.STEP, manifest_id, ledger_seq)

    # ------------------------------------------------------------- internals

    async def _planned_effects(self, session_id: str, target: Checkpoint) -> list[PlannedEffect]:
        """What to do about each effect recorded after ``target``, newest first."""
        records = [
            r for r in await self._checkpoints().records(session_id)
            if r.seq > target.ledger_seq and not r.abandoned and r.reverses is None
        ]
        effects = []
        for record in sorted(records, key=lambda r: r.seq, reverse=True):
            detail = record.error or ""
            if record.category == EffectCategory.RETAINED:
                action = EffectAction.RETAINED
            elif record.category == EffectCategory.IRREVERSIBLE:
                action = EffectAction.IRREVERSIBLE
            elif record.status != EffectStatus.SUCCEEDED:
                action = EffectAction.UNKNOWN
                detail = detail or "the call never reported an outcome"
            elif record.undo_args is None:
                action = EffectAction.CANNOT_UNDO
                detail = "the call result lacked what the undo tool needs"
            else:
                action = EffectAction.UNDO
            effects.append(PlannedEffect(
                record_id=record.record_id, tool_name=record.tool_name, args_summary=record.args_summary,
                action=action, undo_tool=record.undo_tool, undo_args=record.undo_args, detail=detail,
            ))
        return effects

    async def _run_undos(self, session_id: str, rollback_id: str, effects: list[PlannedEffect]) -> list[PlannedEffect]:
        """Run the pending undos in order; after the first failure the rest stay pending."""
        result, stopped = [], False
        for effect in effects:
            if effect.action != EffectAction.UNDO or effect.outcome in (EffectOutcome.UNDONE, EffectOutcome.CANCELLED):
                result.append(effect)
            elif stopped:
                result.append(effect.model_copy(update={"outcome": EffectOutcome.PENDING, "detail": ""}))
            else:
                error = await self._undo(session_id, rollback_id, effect)
                if error is None:
                    result.append(effect.model_copy(update={"outcome": EffectOutcome.UNDONE, "detail": ""}))
                else:
                    stopped = True
                    result.append(effect.model_copy(update={"outcome": EffectOutcome.FAILED, "detail": error}))
        return result

    async def _undo(self, session_id: str, rollback_id: str, effect: PlannedEffect) -> str | None:
        """Run one undo through the agent's tool environment, without the model. Return an error or None.

        The undo is itself recorded, keyed by the record it reverses and the
        rollback, so a retry never runs a successful undo twice.
        """
        store = self._checkpoints()
        previous = [
            r for r in await store.records(session_id)
            if r.reverses == effect.record_id and r.rollback_id == rollback_id
        ]
        if any(r.status == EffectStatus.SUCCEEDED for r in previous):
            return None
        if any(r.status == EffectStatus.INTENDED for r in previous):
            return "an earlier attempt was interrupted and its outcome is unknown; check it by hand"
        async with self._ledger_lock(session_id):
            record = LedgerRecord(
                record_id=uuid.uuid4().hex, session_id=session_id, seq=await store.next_ledger_seq(session_id),
                tool_call_id="", tool_name=effect.undo_tool or "", args_summary=_summary(effect.undo_args),
                category=self._declarations.get(effect.undo_tool or "").category,
                reverses=effect.record_id, rollback_id=rollback_id,
            )
            await store.put_record(record)
        error: str | None = None
        try:
            result = await self._invoke_tool(effect.undo_tool or "", dict(effect.undo_args or {}))
            if is_failure(result):
                error = f"{effect.undo_tool} reported failure: {_summary(result)[:200]}"
        except Exception as exc:  # Any failure stops the remaining undos; report it.
            error = f"{effect.undo_tool} raised {type(exc).__name__}: {exc}"[:300]
        status = EffectStatus.FAILED if error else EffectStatus.SUCCEEDED
        async with self._ledger_lock(session_id):
            await store.put_record(record.model_copy(update={
                "status": status, "error": error, "completed_at": time.time(),
            }))
        return error

    async def _invoke_tool(self, tool_name: str, args: dict) -> object:
        """Call a tool of the served agent directly, in the agent's execution environment."""
        from openjiuwen.core.runner import Runner

        deep_agent = self._rail.deep_agent
        react_agent = deep_agent.react_agent if deep_agent is not None else None
        if react_agent is None:
            raise build_error(
                StatusCode.DEEPAGENT_RUNTIME_ERROR,
                error_msg="the agent is not initialized; call DeepAgent.ensure_initialized() first",
            )
        manager = react_agent.ability_manager
        card = manager.get(tool_name)
        tool = Runner.resource_mgr.get_tool(tool_id=card.id or card.name) if card is not None else None
        if tool is None:
            raise build_error(
                StatusCode.DEEPAGENT_RUNTIME_ERROR, error_msg=f"undo tool {tool_name} is not available to the agent",
            )
        resolver = manager.execution_environment_resolver
        environment = await resolver() if resolver is not None else ExecutionEnvironment()
        with execution_environment_scope(environment):
            return await tool.invoke(args)

    async def _abandon_records(self, session_id: str, target: Checkpoint) -> None:
        """Mark the records after ``target`` as belonging to a discarded path."""
        store = self._checkpoints()
        async with self._ledger_lock(session_id):
            for record in await store.records(session_id):
                if record.seq > target.ledger_seq and not record.abandoned:
                    await store.put_record(record.model_copy(update={"abandoned": True}))

    @staticmethod
    def _status(effects: list[PlannedEffect], unrestored: list[str]) -> RollbackStatus:
        unfinished = any(e.outcome in (EffectOutcome.FAILED, EffectOutcome.PENDING) for e in effects)
        return RollbackStatus.PARTIAL if unfinished or unrestored else RollbackStatus.COMPLETED

    async def _partial_plan(self, plan_id: str) -> RollbackPlan:
        plan = await self._checkpoints().get_plan(plan_id)
        if plan is None or plan.status != RollbackStatus.PARTIAL:
            raise build_error(
                StatusCode.DEEPAGENT_INPUT_PARAM_ERROR, error_msg=f"rollback {plan_id} is not partial",
            )
        return plan

    async def _add_note(self, session_id: str, note: str) -> None:
        """Queue a note for the agent's next round, after any pending rollback's notes."""
        store = self._checkpoints()
        pending = await store.get_pending(session_id)
        if pending is None:
            pending = PendingRollback(session_id=session_id, target_id=None)
        await store.put_pending(pending.model_copy(update={"notes": [*pending.notes, note]}))

    async def _restore(self, session: "Session", deep_agent: "DeepAgent") -> None:
        session_id = session.get_session_id()
        context_engine = deep_agent.react_agent.context_engine
        store = self._checkpoints()
        pending = await store.get_pending(session_id)
        if pending is not None:
            if pending.target_id is not None:
                await self._apply_pending(store, session, deep_agent, pending)
                return
        head = await store.head(session_id)
        if head is not None and _live_is_empty(session, context_engine):
            await self._manager_for(session, context_engine).rewind(head.commit_id)
            deep_agent.clear_state(session)
            logger.info("restored session %s from checkpoint #%s", session_id, head.seq)
        if pending is not None:  # Notes only: the round-start checkpoint records them.
            await self._append_note(session, context_engine, pending.notes)
            await store.clear_pending(session_id)

    @staticmethod
    async def _append_note(session: "Session", context_engine: "ContextEngine", notes: list[str]) -> None:
        context = context_engine.get_context(session_id=session.get_session_id())
        if context is None:
            context = await context_engine.create_context(session=session)
        await context.add_messages(
            UserMessage(
                content="\n\n".join(notes),
                metadata={OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: ROLLBACK_NOTE_SOURCE_KIND},
            )
        )

    async def _apply_pending(
        self, store: CheckpointStore, session: "Session", deep_agent: "DeepAgent", pending: PendingRollback,
    ) -> None:
        session_id = session.get_session_id()
        context_engine = deep_agent.react_agent.context_engine
        target = await store.get(session_id, pending.target_id)
        if target is None:
            raise build_error(
                StatusCode.DEEPAGENT_RUNTIME_ERROR,
                error_msg=f"rollback target {pending.target_id} of session {session_id} is missing",
            )
        manager = self._manager_for(session, context_engine)
        await manager.rewind(target.commit_id)
        deep_agent.clear_state(session)
        await self._append_note(session, context_engine, pending.notes)
        manifest_id = pending.manifest_id if pending.manifest_id is not None else target.manifest_id
        await self._commit(
            store, manager, session_id, target, CheckpointKind.ROLLBACK, manifest_id,
            await store.ledger_seq(session_id),
        )
        await store.clear_pending(session_id)
        logger.info("rollback written back: session=%s target=#%s", session_id, target.seq)

    async def _load_manifest(self, session_id: str, manifest_id: str | None) -> tuple[Manifest, int] | None:
        """The manifest stored as ``manifest_id`` and its delta depth; ``None`` when missing."""
        if manifest_id is None:
            return None
        store = self._checkpoints()
        records: list[ManifestRecord] = []
        current: str | None = manifest_id
        while current is not None:
            record = await store.get_manifest(session_id, current)
            if record is None:
                return None
            records.append(record)
            current = record.parent_id
        manifest: Manifest | None = None
        for record in reversed(records):
            manifest = record.apply(manifest)
        return manifest, records[0].depth

    async def _files_ready(self, session_id: str) -> _Files | None:
        """The session's snapshot state with a manifest loaded, once any baseline is done."""
        if self._workspace is None:
            return None
        files = self._files.setdefault(session_id, _Files())
        if files.baseline is not None:
            await files.baseline
        if files.manifest is None:
            head = await self._checkpoints().head(session_id)
            loaded = await self._load_manifest(session_id, head.manifest_id) if head is not None else None
            if loaded is not None:
                files.manifest, files.depth = loaded
                files.manifest_id = head.manifest_id
        return files

    async def _prepare_files(self, session_id: str) -> None:
        """Load the session's latest manifest, or start its first snapshot in the background."""
        if self._workspace is None:
            return
        files = self._files.setdefault(session_id, _Files())
        if files.manifest is not None or files.baseline is not None:
            return
        head = await self._checkpoints().head(session_id)
        loaded = await self._load_manifest(session_id, head.manifest_id) if head is not None else None
        if loaded is not None:
            files.manifest, files.depth = loaded
            files.manifest_id = head.manifest_id
            return
        files.manifest_id, files.depth = uuid.uuid4().hex, 0
        files.baseline = asyncio.create_task(self._baseline(session_id, files))

    async def _baseline(self, session_id: str, files: _Files) -> None:
        workspace = self._workspace
        store = self._checkpoints()
        try:
            seed_record = await store.get_workspace_scan(self._workspace_key)
            seed = seed_record.apply(None) if seed_record is not None else None
            manifest = await asyncio.to_thread(workspace.scan, seed)
            await store.put_manifest(session_id, ManifestRecord.build(files.manifest_id, manifest, None))
            files.manifest = manifest
            await store.put_workspace_scan(self._workspace_key, ManifestRecord.build("scan", manifest, None))
            if manifest.skipped():
                logger.warning(
                    "recoverable: these files are over the size cap and cannot be rolled back: %s",
                    _listed(manifest.skipped()),
                )
        except Exception:  # The agent keeps working; checkpoints just lack this snapshot.
            logger.exception("recoverable: first workspace snapshot of session %s failed", session_id)
            files.manifest, files.manifest_id = None, None
        finally:
            files.baseline = None

    async def _snapshot(self, session_id: str) -> str | None:
        """Scan the workspace and store a manifest when its content changed; return the current id."""
        if self._workspace is None:
            return None
        files = self._files.setdefault(session_id, _Files())
        if files.baseline is not None:
            return files.manifest_id  # Nothing ran yet that could change files; tools wait for it.
        manifest = await asyncio.to_thread(self._workspace.scan, files.manifest)
        if files.manifest is not None and files.manifest_id is not None and manifest.same_content(files.manifest):
            files.manifest = manifest
            return files.manifest_id
        base = (files.manifest_id, files.depth, files.manifest) if files.manifest_id and files.manifest else None
        record = ManifestRecord.build(uuid.uuid4().hex, manifest, base)
        await self._checkpoints().put_manifest(session_id, record)
        files.manifest, files.manifest_id, files.depth = manifest, record.manifest_id, record.depth
        return record.manifest_id

    async def _restore_workspace(self, session_id: str, target: Checkpoint) -> tuple[RestoreReport | None, str | None]:
        """Make the workspace match ``target``; return the report and the manifest id afterwards."""
        files = await self._files_ready(session_id)
        loaded = await self._load_manifest(session_id, target.manifest_id)
        if files is None or loaded is None:
            return None, files.manifest_id if files is not None else None
        target_manifest, target_depth = loaded
        workspace = self._workspace
        current = await asyncio.to_thread(workspace.scan, files.manifest)
        report = await asyncio.to_thread(workspace.restore, current, target_manifest)
        after = await asyncio.to_thread(workspace.scan, current)
        if report.failed:  # Retry once: a file may have been busy.
            retry = await asyncio.to_thread(workspace.restore, after, target_manifest)
            report.written.extend(retry.written)
            report.deleted.extend(retry.deleted)
            report.failed = retry.failed
            after = await asyncio.to_thread(workspace.scan, after)
        if after.same_content(target_manifest):
            files.manifest, files.manifest_id, files.depth = after, target.manifest_id, target_depth
        else:
            record = ManifestRecord.build(uuid.uuid4().hex, after, (target.manifest_id, target_depth, target_manifest))
            await self._checkpoints().put_manifest(session_id, record)
            files.manifest, files.manifest_id, files.depth = after, record.manifest_id, record.depth
        logger.info(
            "recoverable: workspace restored to #%s: %s written, %s deleted, %s not restored",
            target.seq, len(report.written), len(report.deleted), len(report.unrecoverable) + len(report.failed),
        )
        return report, files.manifest_id

    @staticmethod
    async def _commit(
        store: CheckpointStore,
        manager: VersioningManager,
        session_id: str,
        parent: Checkpoint | None,
        kind: CheckpointKind,
        manifest_id: str | None,
        ledger_seq: int,
    ) -> Checkpoint:
        commit_id = await manager.commit(kind.value)
        checkpoint = Checkpoint(
            checkpoint_id=uuid.uuid4().hex,
            session_id=session_id,
            seq=parent.seq + 1 if parent is not None else 0,
            parent_id=parent.checkpoint_id if parent is not None else None,
            commit_id=commit_id,
            event_id=manager.current_head().event_id,
            manifest_id=manifest_id,
            ledger_seq=ledger_seq,
            kind=kind,
        )
        await store.put(checkpoint)
        await store.set_head(session_id, checkpoint.checkpoint_id)
        return checkpoint
