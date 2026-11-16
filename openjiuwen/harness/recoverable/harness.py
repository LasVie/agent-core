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
not run) and completed with their outcome after. Outside files a shell or code
call declares are backed up into the same record. A rollback undoes the
reversible effects and puts the outside files back, newest first, before it
restores the workspace; it stops at the first undo that fails and reports the
rollback as partial.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import uuid
import weakref
from dataclasses import dataclass, field
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
from openjiuwen.harness.recoverable.dependencies import (
    PackageChange,
    Packages,
    diff_packages,
    env_kind,
    fingerprint,
    global_manager,
    global_undo,
    list_global,
    list_packages,
    restore_environment,
)
from openjiuwen.harness.recoverable.effects import (
    DEFAULT_OUTWARD_COMMANDS,
    EXTERNAL_PATHS_ARG,
    PER_CALL_TOOLS,
    SHELL_TOOL,
    SIDE_EFFECT_ARG,
    DeclarationTable,
    EffectCategory,
    EffectDeclaration,
    build_undo_args,
    declaration_schema,
    is_failure,
    parse_args,
    result_data,
)
from openjiuwen.harness.recoverable.external import back_up_paths, put_back, record_after
from openjiuwen.harness.recoverable.manifest import Manifest, ManifestRecord
from openjiuwen.harness.recoverable.models import (
    Checkpoint,
    CheckpointKind,
    EffectAction,
    EffectOutcome,
    EffectStatus,
    Initiator,
    LedgerRecord,
    PackageChangeInfo,
    PathBackup,
    PendingRollback,
    PlannedEffect,
    RollbackApproval,
    RollbackPlan,
    RollbackResult,
    RollbackStatus,
)
from openjiuwen.harness.recoverable.objects import ObjectStore
from openjiuwen.harness.recoverable.processes import process_start_time, stop_process_group
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
_GLOBAL_KEY = "_recoverable_global_packages"
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
        ("Outside files left as they are because they changed again after the agent's call",
         [e for e in effects if e.outcome == EffectOutcome.KEPT]),
        ("Undo FAILED or was not attempted; still in effect",
         [e for e in effects if e.outcome in (EffectOutcome.FAILED, EffectOutcome.PENDING)]),
        ("Could NOT be undone and still in effect", [e for e in effects if e.action == EffectAction.IRREVERSIBLE]),
        ("Could NOT be undone (the information needed was missing)",
         [e for e in effects if e.action == EffectAction.CANNOT_UNDO]),
        ("Outcome unknown, not undone", [e for e in effects if e.action == EffectAction.UNKNOWN]),
        ("Failed calls whose undo was not run, as nobody confirmed it",
         [e for e in effects if e.action == EffectAction.REVIEW and e.outcome is None]),
        ("Kept on purpose", [e for e in effects if e.action == EffectAction.RETAINED]),
    ]
    lines = [f"{title}: {_listed([_label(e) for e in group])}." for title, group in groups if group]
    if not effects:
        lines.append("No effects outside the workspace were recorded after that checkpoint.")
    lines.append("Effects the harness did not record, such as those inside subagents or outside files a call "
                 "did not declare, were not handled.")
    return lines


def _package_lines(changes: list[PackageChangeInfo]) -> list[str]:
    lines = []
    for change in changes:
        what = ", ".join(
            [f"removed {name}" for name in change.remove] + [f"installed {n}=={v}" for n, v in change.install.items()]
        )
        if change.error:
            lines.append(f"Dependencies in {change.env} could NOT be restored ({change.error}); still needed: {what}.")
        else:
            lines.append(f"Dependencies in {change.env} restored: {what}.")
    return lines


def _rollback_note(
    plan: RollbackPlan, target: Checkpoint, abandoned: int, report: RestoreReport | None,
    effects: list[PlannedEffect], stopped: dict[int, str], packages: list[PackageChangeInfo],
) -> str:
    who = "The user" if plan.initiator == Initiator.HUMAN else (plan.source or "An automatic monitor")
    lines = [
        f"[ROLLBACK] {who} rolled this conversation back to checkpoint #{target.seq}.",
        f"The {abandoned} later checkpoint(s) were discarded, with their model replies, tool calls and tool results.",
    ]
    if stopped:
        lines.append("Background processes you started after it: "
                     + ", ".join(f"pid {pid} {outcome}" for pid, outcome in sorted(stopped.items())) + ".")
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
        lines.extend(_package_lines(packages))
        lines.append("Memory was kept as it is.")
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
    packages: dict[str, Packages] | None = None
    """Latest package list of each dependency environment, by workspace-relative path."""
    fingerprints: dict[str, str] = field(default_factory=dict)
    packages_id: str | None = None


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
        outward_commands: Programs whose shell calls count as irreversible even
            when the model declares them without effect.
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
        outward_commands: frozenset[str] = DEFAULT_OUTWARD_COMMANDS,
    ):
        self._storage_dir = Path(storage_dir).expanduser().resolve() if storage_dir else DEFAULT_STORAGE_DIR
        self._kv_store = kv_store
        self._store: CheckpointStore | None = None
        self._dependency_dirs = frozenset(dependency_dirs)
        self._build_output_dirs = frozenset(build_output_dirs)
        self._file_size_cap = file_size_cap
        self._use_clones = use_clones
        self._declarations = DeclarationTable(declarations, frozenset(outward_commands))
        self._workspace: WorkspaceSnapshot | None = None
        self._outside_objects: ObjectStore | None = None
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
            self._workspace, self._outside_objects = None, None
            logger.warning("recoverable: the workspace is in a sandbox; files will not be rolled back")
            return
        root = Path(workspace_root).expanduser().resolve()
        self._workspace_key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        objects = ObjectStore(
            self._storage_dir / "objects" / self._workspace_key,
            size_cap=self._file_size_cap,
            use_clones=self._use_clones,
        )
        # Same objects; its own instance, so an outside file that cannot be cloned
        # (another volume) does not turn clones off for the workspace.
        self._outside_objects = ObjectStore(
            self._storage_dir / "objects" / self._workspace_key, size_cap=None, use_clones=self._use_clones,
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
            processes_to_stop=[r.pid for r in await self._processes_after(session_id, target)],
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
        changes, created = await self._package_changes(session_id, target, files)
        plan.package_changes = [
            *(PackageChangeInfo(env=env, remove=["(the whole environment)"]) for env in created), *changes,
        ]
        await store.put_plan(plan)
        return plan

    async def apply_rollback(self, approval: RollbackApproval) -> RollbackResult:
        """Apply a confirmed plan between rounds; the context follows at the next round.

        Order: stop background processes, undo the recorded effects newest first,
        restore the dependency environments, restore the workspace files, then
        discard the later checkpoints. The first undo that fails stops the
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
            # Stop what the agent left running first: it could keep writing during the restore.
            stopped = await self._stop_processes(session_id, target)
            # Recompute: effects recorded after the plan was made are undone too.
            effects = self._apply_choices(await self._planned_effects(session_id, target), approval)
            effects = await self._run_undos(session_id, plan.plan_id, effects)
            # Dependencies before files: uninstalling can touch files such as package.json.
            packages = await self._restore_dependencies(session_id, target)
            report, manifest_id = await self._restore_workspace(session_id, target)
            await self._abandon_records(session_id, target)
            pending = await store.get_pending(session_id)
            notes = [*pending.notes] if pending is not None else []
            notes.append(_rollback_note(plan, target, len(abandoned), report, effects, stopped, packages))
            files = self._files.get(session_id)
            await store.put_pending(PendingRollback(
                session_id=session_id, target_id=target.checkpoint_id, manifest_id=manifest_id,
                packages_id=files.packages_id if files is not None else None, notes=notes,
            ))
            await store.set_head(session_id, target.checkpoint_id)
            await store.delete(session_id, abandoned)
            kept_manifests = {c.manifest_id for c in chain[index:]} | {manifest_id}
            await store.delete_manifests(
                session_id, sorted({c.manifest_id for c in chain[:index]} - kept_manifests - {None}),
            )
            unrestored = sorted({*report.unrecoverable, *report.failed}) if report else []
            plan = plan.model_copy(update={
                "applied_at": time.time(), "effects": effects, "package_changes": packages,
                "status": self._status(effects, unrestored, packages),
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
            processes_stopped=sorted(pid for pid, outcome in stopped.items() if outcome == "stopped"),
            package_changes=packages,
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
            packages = await self._restore_dependencies(session_id, target)
            report, _ = await self._restore_workspace(session_id, target)
            unrestored = sorted({*report.unrecoverable, *report.failed}) if report else []
            plan = plan.model_copy(update={
                "effects": effects, "package_changes": packages, "status": self._status(effects, unrestored, packages),
            })
            await store.put_plan(plan)
            await self._add_note(session_id, "[ROLLBACK] A partial rollback was retried.\n"
                                 + "\n".join(_package_lines(packages) + _effect_lines(effects)))
        return RollbackResult(
            plan_id=plan.plan_id,
            session_id=session_id,
            target_id=plan.target_id,
            abandoned_ids=plan.abandoned_ids,
            status=plan.status,
            effects=effects,
            package_changes=packages,
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
        before it runs, with a backup of the outside files it declares. If the record
        cannot be written, the call is skipped with an error result: an effect
        nobody recorded could never be rolled back.
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
        args = parse_args(getattr(inputs, "tool_args", None))
        call = self._declarations.for_call(tool_name, args)
        # Recorded even without effect: rollback stops a background process and reverses global installs.
        background = tool_name == SHELL_TOOL and bool(args.get("run_in_background"))
        manager = global_manager(str(args.get("command") or "")) if tool_name == SHELL_TOOL else None
        paths = args.get(EXTERNAL_PATHS_ARG) if self._declarations.declared_per_call(tool_name) else None
        declared = [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []
        if call.declaration.category == EffectCategory.NONE and not background and manager is None and not declared:
            return
        if manager is not None:
            ctx.extra[_GLOBAL_KEY] = (manager, await asyncio.to_thread(list_global, manager))
        session_id = session.get_session_id()
        try:
            backups = await asyncio.to_thread(self._back_up, declared, args) if declared else []
            store = self._checkpoints()
            async with self._ledger_lock(session_id):
                record = LedgerRecord(
                    record_id=uuid.uuid4().hex,
                    session_id=session_id,
                    seq=await store.next_ledger_seq(session_id),
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    args_summary=_summary(args),
                    category=call.declaration.category,
                    detail=call.detail,
                    external_paths=declared,
                    external_backups=backups,
                )
                await store.put_record(record)
        except Exception:  # Fail closed: the call must not run unrecorded.
            logger.exception("recoverable: cannot record the intent of %s; the call is skipped", tool_name)
            inputs.tool_result = {"success": False, "error": _UNRECORDED}
            inputs.tool_msg = ToolMessage(content=f"Error: {_UNRECORDED}.", tool_call_id=call_id)
            ctx.extra.setdefault("_skip_tool_calls", {})[call_id] = True
            return
        ctx.extra[_RECORD_KEY] = record

    def _back_up(self, declared: list[str], args: dict) -> list[PathBackup]:
        """Back up the outside paths a call declared; in a sandbox they are not on this machine."""
        if self._workspace is None or self._outside_objects is None:
            return [PathBackup(path=path, error="the workspace runs in a sandbox, outside this machine")
                    for path in declared]
        cwd = Path(str(args.get("workdir") or self._workspace.root))
        return back_up_paths(declared, cwd, self._workspace.root, self._storage_dir, self._outside_objects,
                             self._file_size_cap)

    async def after_tool(self, ctx: "AgentCallbackContext", exception: BaseException | None) -> None:
        """Complete the call's ledger record with its outcome and, if reversible, its undo."""
        record: LedgerRecord | None = ctx.extra.pop(_RECORD_KEY, None)
        global_before = ctx.extra.pop(_GLOBAL_KEY, None)
        if record is None:
            return
        if isinstance(exception, ToolInterruptException):
            # The call paused before running; its record stays as intended (status unknown).
            return
        inputs = ctx.inputs
        result = getattr(inputs, "tool_result", None)
        args = parse_args(getattr(inputs, "tool_args", None))
        update: dict = {"completed_at": time.time()}
        failed = exception is not None or is_failure(result)
        if failed:
            update.update(status=EffectStatus.FAILED, error=_summary(str(exception) if exception else result)[:200])
        else:
            update["status"] = EffectStatus.SUCCEEDED
        declaration = self._declarations.for_call(record.tool_name, args).declaration
        if declaration.category == EffectCategory.REVERSIBLE:
            # A failed call keeps an undo taken from its arguments; the person decides whether to run it.
            update.update(undo_tool=declaration.undo_tool,
                          undo_args=build_undo_args(declaration, args, None if failed else result))
        data = result_data(result)
        if isinstance(data, dict) and data.get("status") == "started" and isinstance(data.get("pid"), int):
            update.update(pid=data["pid"], pid_started=await asyncio.to_thread(process_start_time, data["pid"]))
        if record.external_backups:  # Even after a failure: the call may have written part of them.
            update["external_backups"] = await asyncio.to_thread(
                lambda: [record_after(backup, self._file_size_cap) for backup in record.external_backups]
            )
        if global_before is not None and global_before[1] is not None:
            # Compare what is installed now with before: exact even when the command failed halfway.
            manager, before = global_before
            after = await asyncio.to_thread(list_global, manager)
            if after is not None:
                update["generated_undo"], update["generated_cannot"] = global_undo(manager, before, after)
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
            manifest_id, packages_id = await self._snapshot(session_id)
            ledger_seq = await store.ledger_seq(session_id)
            head = await store.head(session_id)
            event_id = manager.current_head().event_id
            if head is not None and (head.event_id, head.manifest_id, head.packages_id, head.ledger_seq) == (
                event_id, manifest_id, packages_id, ledger_seq,
            ):
                return head
            if head is None and event_id == 0:
                return None
            return await self._commit(
                store, manager, session_id, head, CheckpointKind.STEP, manifest_id, ledger_seq, packages_id,
            )

    # ------------------------------------------------------------- internals

    async def _planned_effects(self, session_id: str, target: Checkpoint) -> list[PlannedEffect]:
        """What to do about each effect recorded after ``target``, newest first."""
        records = [
            r for r in await self._checkpoints().records(session_id)
            if r.seq > target.ledger_seq and not r.abandoned and r.reverses is None
        ]
        effects = []
        for record in sorted(records, key=lambda r: r.seq, reverse=True):
            if record.category != EffectCategory.NONE:  # A background launch alone: its process is stopped instead.
                effects.append(self._planned_effect(record))
            # The harness's own undo of global installs runs after the model's reverse command.
            if record.generated_undo:
                effects.append(PlannedEffect(
                    record_id=f"{record.record_id}:global", tool_name=f"{record.tool_name} (global packages)",
                    args_summary=record.generated_undo, action=EffectAction.UNDO, undo_tool=SHELL_TOOL,
                    undo_args={"command": record.generated_undo},
                ))
            if record.generated_cannot:
                effects.append(PlannedEffect(
                    record_id=f"{record.record_id}:global-cannot", tool_name=f"{record.tool_name} (global packages)",
                    args_summary="; ".join(record.generated_cannot), action=EffectAction.CANNOT_UNDO,
                    detail="the old version cannot be installed again",
                ))
            # Outside files last: a reverse command may still read them.
            effects.extend(self._path_effect(record, index, backup)
                           for index, backup in enumerate(record.external_backups))
        return effects

    @staticmethod
    def _path_effect(record: LedgerRecord, index: int, backup: PathBackup) -> PlannedEffect:
        """What a rollback does about one outside path a call declared."""
        effect = PlannedEffect(
            record_id=f"{record.record_id}:path:{index}", tool_name=f"{record.tool_name} (outside file)",
            args_summary=backup.path, action=EffectAction.UNDO, path=backup.path,
            detail="put back as it was before the call" if backup.before is not None
            else "did not exist before the call; it is deleted",
        )
        if backup.error is not None:
            return effect.model_copy(update={
                "action": EffectAction.IRREVERSIBLE, "path": None, "detail": f"not backed up: {backup.error}",
            })
        if not backup.after_taken:
            return effect.model_copy(update={
                "action": EffectAction.REVIEW,
                "detail": "what the call left there was not recorded, so a later change by someone else cannot be "
                          "told apart; putting it back overwrites any such change",
            })
        return effect

    @staticmethod
    def _planned_effect(record: LedgerRecord) -> PlannedEffect:
        """What a rollback does about one record's own effect."""
        detail = record.detail or record.error or ""
        if record.category == EffectCategory.RETAINED:
            action = EffectAction.RETAINED
        elif record.category == EffectCategory.IRREVERSIBLE:
            action = EffectAction.IRREVERSIBLE
        elif record.status == EffectStatus.FAILED and record.undo_args is not None:
            action = EffectAction.REVIEW
            detail = f"the call failed ({record.error or 'no detail'}); its undo was written for a success"
        elif record.status != EffectStatus.SUCCEEDED:
            action = EffectAction.UNKNOWN
            detail = detail or "the call never reported an outcome"
        elif record.undo_args is None:
            action = EffectAction.CANNOT_UNDO
            detail = "the call result or arguments lacked what the undo needs"
        else:
            action = EffectAction.UNDO
        return PlannedEffect(
            record_id=record.record_id, tool_name=record.tool_name, args_summary=record.args_summary,
            action=action, undo_tool=record.undo_tool, undo_args=record.undo_args, detail=detail,
        )

    @staticmethod
    def _apply_choices(effects: list[PlannedEffect], approval: RollbackApproval) -> list[PlannedEffect]:
        """Strike the undos the person cancelled; promote the reviewed ones they confirmed."""
        cancelled, confirmed = set(approval.cancelled_effect_ids), set(approval.confirmed_effect_ids)
        chosen = []
        for effect in effects:
            if effect.action == EffectAction.REVIEW and effect.record_id in confirmed:
                effect = effect.model_copy(update={"action": EffectAction.UNDO})
            if effect.action == EffectAction.UNDO and effect.record_id in cancelled:
                effect = effect.model_copy(update={"outcome": EffectOutcome.CANCELLED})
            chosen.append(effect)
        return chosen

    async def _processes_after(self, session_id: str, target: Checkpoint) -> list[LedgerRecord]:
        return [
            r for r in await self._checkpoints().records(session_id)
            if r.seq > target.ledger_seq and not r.abandoned and r.pid is not None
        ]

    async def _stop_processes(self, session_id: str, target: Checkpoint) -> dict[int, str]:
        """Stop the background processes started after ``target``; pid to what happened."""
        stopped = {}
        for record in await self._processes_after(session_id, target):
            stopped[record.pid] = await asyncio.to_thread(stop_process_group, record.pid, record.pid_started)
            logger.info("recoverable: background pid %s %s", record.pid, stopped[record.pid])
        return stopped

    def _blocked_reverse_command(self, command: str) -> str | None:
        """Check a model-written reverse command again before it runs unattended."""
        from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
        from openjiuwen.harness.security.permission_engine.models import PermissionLevel
        from openjiuwen.harness.tools.shell.bash._security import check_injection

        injection = check_injection(command)
        if injection.blocked:
            return f"the reverse command was blocked: {injection.reason}"
        deep_agent = self._rail.deep_agent
        for rail in deep_agent.configured_rails() if deep_agent is not None else []:
            if isinstance(rail, PermissionInterruptRail):
                level, rule = rail.engine.evaluate_global_policy_directly(SHELL_TOOL, {"command": command})
                if level == PermissionLevel.DENY:
                    return f"the reverse command is denied by the permission policy ({rule})"
        return None

    def add_declaration_params(self, deep_agent: "DeepAgent") -> None:
        """Let the model declare each shell and code call: add the declaration arguments to their schemas."""
        react_agent = deep_agent.react_agent
        if react_agent is None:
            return
        config = deep_agent.deep_config
        language = "en" if str(getattr(config, "language", "") or "").lower().startswith("en") else "cn"
        for name in PER_CALL_TOOLS:
            card = react_agent.ability_manager.get(name) if self._declarations.declared_per_call(name) else None
            params = getattr(card, "input_params", None)
            if not isinstance(params, dict) or SIDE_EFFECT_ARG in params.get("properties", {}):
                continue
            params.setdefault("properties", {}).update(declaration_schema(language))
            required = params.setdefault("required", [])
            if SIDE_EFFECT_ARG not in required:
                required.append(SIDE_EFFECT_ARG)

    async def _run_undos(self, session_id: str, rollback_id: str, effects: list[PlannedEffect]) -> list[PlannedEffect]:
        """Run the pending undos in order; after the first failure the rest stay pending."""
        result, stopped = [], False
        done = (EffectOutcome.UNDONE, EffectOutcome.CANCELLED, EffectOutcome.KEPT)
        for effect in effects:
            if effect.action != EffectAction.UNDO or effect.outcome in done:
                result.append(effect)
            elif stopped:
                result.append(effect.model_copy(update={"outcome": EffectOutcome.PENDING, "detail": ""}))
            else:
                if effect.path is not None:
                    outcome, detail = await self._put_back(session_id, effect)
                else:
                    error = await self._undo(session_id, rollback_id, effect)
                    outcome, detail = (EffectOutcome.FAILED, error) if error else (EffectOutcome.UNDONE, "")
                stopped = outcome == EffectOutcome.FAILED
                result.append(effect.model_copy(update={"outcome": outcome, "detail": detail}))
        return result

    async def _put_back(self, session_id: str, effect: PlannedEffect) -> tuple[EffectOutcome, str]:
        """Put one outside path back from its call's backup."""
        record_id = effect.record_id.split(":", 1)[0]
        record = next((r for r in await self._checkpoints().records(session_id) if r.record_id == record_id), None)
        backup = next((b for b in record.external_backups if b.path == effect.path), None) if record else None
        if backup is None or self._outside_objects is None:
            return EffectOutcome.FAILED, "the backup of this path is not available"
        # A retry after a failed attempt skips the check: that attempt may have changed the path itself.
        check_after = backup.after_taken and effect.outcome != EffectOutcome.FAILED
        outcome, detail = await asyncio.to_thread(put_back, backup, self._outside_objects, check_after=check_after)
        logger.info("recoverable: outside path %s %s %s", backup.path, outcome.value, detail)
        return outcome, detail

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
        if effect.undo_tool == SHELL_TOOL:
            blocked = self._blocked_reverse_command(str((effect.undo_args or {}).get("command") or ""))
            if blocked is not None:
                return blocked
        async with self._ledger_lock(session_id):
            record = LedgerRecord(
                record_id=uuid.uuid4().hex, session_id=session_id, seq=await store.next_ledger_seq(session_id),
                tool_call_id="", tool_name=effect.undo_tool or "", args_summary=_summary(effect.undo_args),
                category=self._declarations.get(effect.undo_tool or "").category,
                reverses=effect.record_id, rollback_id=rollback_id,
            )
            await store.put_record(record)
        undo_args = dict(effect.undo_args or {})
        if effect.undo_tool == SHELL_TOOL and self._workspace is not None:
            undo_args.setdefault("workdir", str(self._workspace.root))  # Between rounds no cwd is bound.
        error: str | None = None
        try:
            result = await self._invoke_tool(effect.undo_tool or "", undo_args)
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
    def _status(
        effects: list[PlannedEffect], unrestored: list[str], packages: list[PackageChangeInfo],
    ) -> RollbackStatus:
        unfinished = any(e.outcome in (EffectOutcome.FAILED, EffectOutcome.PENDING) for e in effects)
        failed_envs = any(change.error for change in packages)
        return RollbackStatus.PARTIAL if unfinished or unrestored or failed_envs else RollbackStatus.COMPLETED

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
        packages_id = pending.packages_id if pending.packages_id is not None else target.packages_id
        await self._commit(
            store, manager, session_id, target, CheckpointKind.ROLLBACK, manifest_id,
            await store.ledger_seq(session_id), packages_id,
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
            await self._load_head_files(session_id, files)
        return files

    async def _load_head_files(self, session_id: str, files: _Files) -> bool:
        """Load HEAD's manifest and package lists into ``files``; False when HEAD has no manifest."""
        store = self._checkpoints()
        head = await store.head(session_id)
        loaded = await self._load_manifest(session_id, head.manifest_id) if head is not None else None
        if loaded is None:
            return False
        files.manifest, files.depth = loaded
        files.manifest_id = head.manifest_id
        if head.packages_id is not None:
            files.packages = await store.get_packages(session_id, head.packages_id)
            files.packages_id = head.packages_id if files.packages is not None else None
        return True

    async def _prepare_files(self, session_id: str) -> None:
        """Load the session's latest manifest, or start its first snapshot in the background."""
        if self._workspace is None:
            return
        files = self._files.setdefault(session_id, _Files())
        if files.manifest is not None or files.baseline is not None:
            return
        if await self._load_head_files(session_id, files):
            return
        files.manifest_id, files.depth = uuid.uuid4().hex, 0
        files.packages_id = uuid.uuid4().hex
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
            await self._snapshot_packages(session_id, files, manifest.dependency_dirs)
            await store.put_workspace_scan(self._workspace_key, ManifestRecord.build("scan", manifest, None))
            if manifest.skipped():
                logger.warning(
                    "recoverable: these files are over the size cap and cannot be rolled back: %s",
                    _listed(manifest.skipped()),
                )
        except Exception:  # The agent keeps working; checkpoints just lack this snapshot.
            logger.exception("recoverable: first workspace snapshot of session %s failed", session_id)
            files.manifest, files.manifest_id, files.packages, files.packages_id = None, None, None, None
        finally:
            files.baseline = None

    async def _snapshot(self, session_id: str) -> tuple[str | None, str | None]:
        """Scan the workspace; store a manifest and package lists that changed. Return their ids."""
        if self._workspace is None:
            return None, None
        files = self._files.setdefault(session_id, _Files())
        if files.baseline is not None:
            # Nothing ran yet that could change files; tools wait for it.
            return files.manifest_id, files.packages_id
        manifest = await asyncio.to_thread(self._workspace.scan, files.manifest)
        packages_id = await self._snapshot_packages(session_id, files, manifest.dependency_dirs)
        if files.manifest is not None and files.manifest_id is not None and manifest.same_content(files.manifest):
            files.manifest = manifest
            return files.manifest_id, packages_id
        base = (files.manifest_id, files.depth, files.manifest) if files.manifest_id and files.manifest else None
        record = ManifestRecord.build(uuid.uuid4().hex, manifest, base)
        await self._checkpoints().put_manifest(session_id, record)
        files.manifest, files.manifest_id, files.depth = manifest, record.manifest_id, record.depth
        return record.manifest_id, packages_id

    def _environments(self, dependency_dirs: list[str]) -> list[str]:
        """The dependency directories that are environments whose packages can be listed."""
        return sorted(d for d in dependency_dirs if env_kind(self._workspace.root / d) is not None)

    async def _snapshot_packages(self, session_id: str, files: _Files, dependency_dirs: list[str]) -> str | None:
        """Re-list the environments whose fingerprint changed; store the lists when they differ."""
        root = self._workspace.root
        envs = self._environments(dependency_dirs)
        prints = await asyncio.to_thread(lambda: {env: fingerprint(root / env) for env in envs})
        if files.packages is not None and prints == files.fingerprints:
            return files.packages_id
        stale = [env for env in envs if files.packages is None or env not in files.packages
                 or prints[env] != files.fingerprints.get(env)]
        listed = await asyncio.to_thread(lambda: {env: list_packages(root / env) for env in stale})
        packages = {env: listed[env] if env in listed else files.packages[env] for env in envs}
        files.fingerprints = prints
        if files.packages is not None and files.packages_id is not None and packages == files.packages:
            return files.packages_id
        packages_id = files.packages_id if files.packages is None and files.packages_id else uuid.uuid4().hex
        await self._checkpoints().put_packages(session_id, packages_id, packages)
        files.packages, files.packages_id = packages, packages_id
        return packages_id

    async def _package_changes(
        self, session_id: str, target: Checkpoint, files: _Files | None,
    ) -> tuple[list[PackageChangeInfo], list[str]]:
        """What differs from the target's package lists, and environments created after it."""
        if self._workspace is None or target.packages_id is None:
            return [], []
        wanted = await self._checkpoints().get_packages(session_id, target.packages_id)
        if wanted is None:
            return [], []
        root = self._workspace.root
        manifest = await asyncio.to_thread(self._workspace.scan, files.manifest if files else None)
        envs = self._environments(manifest.dependency_dirs)
        changes, created = [], [env for env in envs if env not in wanted]
        for env in sorted(wanted):
            if env not in envs:
                changes.append(PackageChangeInfo(env=env, install=wanted[env],
                                                 error="the environment no longer exists; recreate it by hand"))
                continue
            diff = diff_packages(env, await asyncio.to_thread(list_packages, root / env), wanted[env])
            if diff.remove or diff.install:
                changes.append(PackageChangeInfo(env=env, remove=diff.remove, install=diff.install))
        return changes, created

    async def _restore_dependencies(self, session_id: str, target: Checkpoint) -> list[PackageChangeInfo]:
        """Bring every dependency environment back to the target's list; delete environments made after it."""
        files = await self._files_ready(session_id)
        changes, created = await self._package_changes(session_id, target, files)
        root = self._workspace.root if self._workspace is not None else None
        done = []
        for env in created:
            await asyncio.to_thread(shutil.rmtree, root / env, True)
            done.append(PackageChangeInfo(env=env, remove=["(the whole environment)"]))
        for change in changes:
            if change.error is None:
                step = PackageChange(env=change.env, remove=change.remove, install=change.install)
                change = change.model_copy(update={"error": await asyncio.to_thread(restore_environment, root, step)})
            done.append(change)
        if files is not None and done:
            manifest = await asyncio.to_thread(self._workspace.scan, files.manifest)
            files.fingerprints = {"": "changed"}  # Matches no real fingerprint: re-list every environment.
            await self._snapshot_packages(session_id, files, manifest.dependency_dirs)
        return done

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
        packages_id: str | None,
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
            packages_id=packages_id,
            ledger_seq=ledger_seq,
            kind=kind,
        )
        await store.put(checkpoint)
        await store.set_head(session_id, checkpoint.checkpoint_id)
        return checkpoint
