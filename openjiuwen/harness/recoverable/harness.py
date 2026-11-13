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
"""

from __future__ import annotations

import asyncio
import hashlib
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
from openjiuwen.harness.recoverable.manifest import Manifest, ManifestRecord
from openjiuwen.harness.recoverable.models import (
    Checkpoint,
    CheckpointKind,
    Initiator,
    PendingRollback,
    RollbackApproval,
    RollbackPlan,
    RollbackResult,
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
    from openjiuwen.harness.deep_agent import DeepAgent

DEFAULT_STORAGE_DIR = Path.home() / ".openjiuwen" / "recoverable"
DEFAULT_FILE_SIZE_CAP = 1 << 30
"""Largest file stored when the file system cannot clone (1 GB)."""
ROLLBACK_NOTE_SOURCE_KIND = "rollback_note"
_DB_FILENAME = "recoverable.db"
_LIST_LIMIT = 10


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


def _rollback_note(plan: RollbackPlan, target: Checkpoint, abandoned: int, report: RestoreReport | None) -> str:
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
        lines.append("Dependency directories (such as node_modules or .venv) were NOT rolled back.")
    lines.append("Effects outside the workspace were NOT rolled back.")
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
    ):
        self._storage_dir = Path(storage_dir).expanduser().resolve() if storage_dir else DEFAULT_STORAGE_DIR
        self._kv_store = kv_store
        self._store: CheckpointStore | None = None
        self._dependency_dirs = frozenset(dependency_dirs)
        self._build_output_dirs = frozenset(build_output_dirs)
        self._file_size_cap = file_size_cap
        self._use_clones = use_clones
        self._workspace: WorkspaceSnapshot | None = None
        self._workspace_key = ""
        self._rail = CheckpointRail(self)
        self._managers: dict[str, tuple[weakref.ref, VersioningManager]] = {}
        self._files: dict[str, _Files] = {}
        self._locks: dict[str, asyncio.Lock] = {}
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

    def bind_workspace(self, workspace_root: str | Path, *, sandboxed: bool) -> None:
        """Snapshot ``workspace_root``; a sandboxed workspace is not on this machine and is skipped."""
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
        """Apply a confirmed plan between rounds: files now, the context at the next round.

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
            if session_id in self._running:
                raise build_error(
                    StatusCode.DEEPAGENT_RUNTIME_ERROR,
                    error_msg=f"session {session_id} is running a round; roll back after it ends",
                )
            chain = await store.chain(session_id)
            ids = [checkpoint.checkpoint_id for checkpoint in chain]
            if plan.target_id not in ids:
                raise build_error(
                    StatusCode.DEEPAGENT_INPUT_PARAM_ERROR,
                    error_msg=f"checkpoint {plan.target_id} is no longer on the history of session {session_id}",
                )
            index = ids.index(plan.target_id)
            target, abandoned = chain[index], ids[:index]
            report, manifest_id = await self._restore_workspace(session_id, target)
            pending = await store.get_pending(session_id)
            notes = [*pending.notes] if pending is not None else []
            notes.append(_rollback_note(plan, target, len(abandoned), report))
            await store.put_pending(PendingRollback(
                session_id=session_id, target_id=target.checkpoint_id, manifest_id=manifest_id, notes=notes,
            ))
            await store.set_head(session_id, target.checkpoint_id)
            await store.delete(session_id, abandoned)
            kept_manifests = {c.manifest_id for c in chain[index:]} | {manifest_id}
            await store.delete_manifests(
                session_id, sorted({c.manifest_id for c in chain[:index]} - kept_manifests - {None}),
            )
            plan = plan.model_copy(update={"applied_at": time.time()})
            await store.put_plan(plan)
            self._managers.pop(session_id, None)
        logger.info("rollback applied: session=%s target=#%s discarded=%s", session_id, target.seq, len(abandoned))
        return RollbackResult(
            plan_id=plan.plan_id,
            session_id=session_id,
            target_id=target.checkpoint_id,
            abandoned_ids=abandoned,
            workspace_restored=report is not None,
            files_written=report.written if report else [],
            files_deleted=report.deleted if report else [],
            build_outputs_deleted=report.build_outputs_deleted if report else [],
            unrestored_files=sorted({*report.unrecoverable, *report.failed}) if report else [],
        )

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

    async def before_tool(self, session: "Session") -> None:
        """Hold a tool call until the session's first workspace snapshot is complete."""
        files = self._files.get(session.get_session_id())
        if files is not None and files.baseline is not None:
            await files.baseline

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
            head = await store.head(session_id)
            event_id = manager.current_head().event_id
            if head is not None and head.event_id == event_id and head.manifest_id == manifest_id:
                return head
            if head is None and event_id == 0:
                return None
            return await self._commit(store, manager, session_id, head, CheckpointKind.STEP, manifest_id)

    # ------------------------------------------------------------- internals

    async def _restore(self, session: "Session", deep_agent: "DeepAgent") -> None:
        session_id = session.get_session_id()
        context_engine = deep_agent.react_agent.context_engine
        store = self._checkpoints()
        pending = await store.get_pending(session_id)
        if pending is not None:
            await self._apply_pending(store, session, deep_agent, pending)
            return
        head = await store.head(session_id)
        if head is None or not _live_is_empty(session, context_engine):
            return
        await self._manager_for(session, context_engine).rewind(head.commit_id)
        deep_agent.clear_state(session)
        logger.info("restored session %s from checkpoint #%s", session_id, head.seq)

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
        context = context_engine.get_context(session_id=session_id)
        if context is None:
            context = await context_engine.create_context(session=session)
        await context.add_messages(
            UserMessage(
                content="\n\n".join(pending.notes),
                metadata={OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA: ROLLBACK_NOTE_SOURCE_KIND},
            )
        )
        manifest_id = pending.manifest_id if pending.manifest_id is not None else target.manifest_id
        await self._commit(store, manager, session_id, target, CheckpointKind.ROLLBACK, manifest_id)
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
            kind=kind,
        )
        await store.put(checkpoint)
        await store.set_head(session_id, checkpoint.checkpoint_id)
        return checkpoint
