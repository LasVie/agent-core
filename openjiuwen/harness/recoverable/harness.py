# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RecoverableHarness: step checkpoints and confirmed rollback for one DeepAgent.

A checkpoint is a set of log positions taken at a step boundary; the context and
DeepAgent state of each one live in vcs. A rollback is planned, confirmed by a
person, then applied between rounds:

1. ``apply_rollback`` deletes the checkpoints after the target, moves HEAD to it
   and leaves a pending rollback.
2. The next round's ``BEFORE_INVOKE`` writes the target context back into the
   live session through vcs ``rewind`` (which also drops the later log), appends
   the rollback note and commits the rollback checkpoint.

The same ``BEFORE_INVOKE`` hook restores the session after a process restart:
when the host reopens a session whose live context is empty, HEAD is written back
before anything new is recorded.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
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
from openjiuwen.harness.recoverable.models import (
    Checkpoint,
    CheckpointKind,
    Initiator,
    PendingRollback,
    RollbackApproval,
    RollbackPlan,
    RollbackResult,
)
from openjiuwen.harness.recoverable.rail import CheckpointRail
from openjiuwen.harness.recoverable.store import CheckpointStore

if TYPE_CHECKING:
    from openjiuwen.core.context_engine import ContextEngine
    from openjiuwen.core.context_engine.base import ModelContext
    from openjiuwen.core.session.agent import Session
    from openjiuwen.harness.deep_agent import DeepAgent

DEFAULT_STORAGE_DIR = Path.home() / ".openjiuwen" / "recoverable"
ROLLBACK_NOTE_SOURCE_KIND = "rollback_note"
_DB_FILENAME = "recoverable.db"


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


def _rollback_note(plan: RollbackPlan, target: Checkpoint, abandoned: int) -> str:
    who = "The user" if plan.initiator == Initiator.HUMAN else (plan.source or "An automatic monitor")
    lines = [
        f"[ROLLBACK] {who} rolled this conversation back to checkpoint #{target.seq}.",
        f"The {abandoned} later checkpoint(s) were discarded, with their model replies, tool calls and tool results.",
        "Files in the workspace and effects outside it were NOT rolled back; check them before relying on them.",
    ]
    if plan.reason:
        lines.append(f"Reason: {plan.reason}")
    return "\n".join(lines)


class RecoverableHarness:
    """Checkpoints every step of a DeepAgent and rolls back to one on confirmation.

    Register :meth:`rail` on the agent. All records persist in ``kv_store``,
    by default a SQLite file under ``storage_dir``, so rollback still works
    after the process restarts.

    Args:
        kv_store: Store for checkpoints and vcs logs. Defaults to SQLite at
            ``storage_dir/recoverable.db``.
        storage_dir: Directory for Recoverable Harness data. It must lie outside
            the agent workspace. Defaults to ``~/.openjiuwen/recoverable``.
    """

    def __init__(self, *, kv_store: BaseKVStore | None = None, storage_dir: str | Path | None = None):
        self._storage_dir = Path(storage_dir).expanduser().resolve() if storage_dir else DEFAULT_STORAGE_DIR
        self._kv_store = kv_store
        self._store: CheckpointStore | None = None
        self._rail = CheckpointRail(self)
        self._managers: dict[str, tuple[weakref.ref, VersioningManager]] = {}
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
        """Work out what rolling ``session_id`` back to ``target_id`` would discard.

        The plan is stored and must be confirmed with :meth:`apply_rollback`.
        Rolling back to HEAD itself discards only what happened after it, such as
        a step that stopped on an interrupt.
        """
        store = self._checkpoints()
        chain = await store.chain(session_id)
        ids = [checkpoint.checkpoint_id for checkpoint in chain]
        if target_id not in ids:
            raise build_error(
                StatusCode.DEEPAGENT_INPUT_PARAM_ERROR,
                error_msg=f"checkpoint {target_id} is not on the history of session {session_id}",
            )
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
        await store.put_plan(plan)
        return plan

    async def apply_rollback(self, approval: RollbackApproval) -> RollbackResult:
        """Apply a confirmed plan. Only between rounds; the context follows at the next round.

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
            pending = await store.get_pending(session_id)
            notes = [*pending.notes] if pending is not None else []
            notes.append(_rollback_note(plan, target, len(abandoned)))
            await store.put_pending(PendingRollback(session_id=session_id, target_id=target.checkpoint_id, notes=notes))
            await store.set_head(session_id, target.checkpoint_id)
            await store.delete(session_id, abandoned)
            plan = plan.model_copy(update={"applied_at": time.time()})
            await store.put_plan(plan)
            self._managers.pop(session_id, None)
        logger.info("rollback applied: session=%s target=#%s discarded=%s", session_id, target.seq, len(abandoned))
        return RollbackResult(
            plan_id=plan.plan_id,
            session_id=session_id,
            target_id=target.checkpoint_id,
            abandoned_ids=abandoned,
        )

    # ------------------------------------------------------- called by the rail

    async def begin_round(self, session: "Session", deep_agent: "DeepAgent") -> None:
        """Mark the session running and write back a pending rollback or a lost session."""
        session_id = session.get_session_id()
        self._running.add(session_id)
        try:
            async with self._lock(session_id):
                await self._restore(session, deep_agent)
        except Exception:
            self._running.discard(session_id)
            raise

    def end_round(self, session: "Session") -> None:
        self._running.discard(session.get_session_id())

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
            head = await store.head(session_id)
            event_id = manager.current_head().event_id
            if head is not None and head.event_id == event_id:
                return head
            if head is None and event_id == 0:
                return None
            return await self._commit(store, manager, session_id, head, CheckpointKind.STEP)

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
        await self._commit(store, manager, session_id, target, CheckpointKind.ROLLBACK)
        await store.clear_pending(session_id)
        logger.info("rollback written back: session=%s target=#%s", session_id, target.seq)

    @staticmethod
    async def _commit(
        store: CheckpointStore,
        manager: VersioningManager,
        session_id: str,
        parent: Checkpoint | None,
        kind: CheckpointKind,
    ) -> Checkpoint:
        commit_id = await manager.commit(kind.value)
        checkpoint = Checkpoint(
            checkpoint_id=uuid.uuid4().hex,
            session_id=session_id,
            seq=parent.seq + 1 if parent is not None else 0,
            parent_id=parent.checkpoint_id if parent is not None else None,
            commit_id=commit_id,
            event_id=manager.current_head().event_id,
            kind=kind,
        )
        await store.put(checkpoint)
        await store.set_head(session_id, checkpoint.checkpoint_id)
        return checkpoint
