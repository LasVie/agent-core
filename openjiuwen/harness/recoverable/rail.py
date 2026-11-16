# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CheckpointRail: the DeepAgent side of Recoverable Harness.

Hooks and where they run (see ``rails/AGENTS.md`` for the routing):

- ``BEFORE_INVOKE`` (outer DeepAgent): mark the session running, write back a
  pending rollback or a session lost to a restart, and start the session's first
  workspace snapshot. Runs first in its chain so no other rail reads the context
  before it is restored.
- ``BEFORE_MODEL_CALL`` (inner ReAct): checkpoint the round start, and any step
  that ended without tool calls but kept the loop going.
- ``BEFORE_TOOL_CALL`` (inner ReAct): wait for the first workspace snapshot, so no
  tool changes a file before its original content is stored; then record the
  intent of a call that may change something outside the workspace. Runs last,
  after permission and interrupt rails have decided whether the call runs.
- ``AFTER_TOOL_CALL`` / ``ON_TOOL_EXCEPTION`` (inner ReAct): record the outcome.
  Runs first, before other rails rewrite the result.
- ``AFTER_REACT_ITERATION`` (inner ReAct): checkpoint the step end.
- ``AFTER_INVOKE`` (outer DeepAgent): checkpoint the round end, unless the round
  raised; mark the session idle.

After the checkpoint at a step boundary (``BEFORE_MODEL_CALL``,
``AFTER_REACT_ITERATION``), a rollback signal that arrived while the agent ran
ends the round through ``request_force_finish``; the round's result names the
signal, and its plan is made when the round ends.

A failed checkpoint is logged and never interrupts the agent; that step just
cannot be a rollback target.

The workspace and sys_operation are injected before ``init``. A sandboxed
sys_operation means the workspace is not on this machine, so files are not
snapshotted.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
from openjiuwen.core.sys_operation.base import OperationMode
from openjiuwen.harness.rails.base import DeepAgentRail

if TYPE_CHECKING:
    from openjiuwen.core.sys_operation import SysOperation
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.recoverable.harness import RecoverableHarness
    from openjiuwen.harness.workspace.workspace import Workspace

# Restore before any other rail's BEFORE_INVOKE reads the session.
_RESTORE_PRIORITY = 10_000
# Read a tool's outcome before any other rail rewrites it.
_OUTCOME_PRIORITY = 1_000_000
# The agent's memory is kept across rollbacks (design A4).
_KEPT_NODES = ("memory", "coding_memory")
ROLLBACK_RESULT_TYPE = "rollback"
"""``result_type`` of a round that ended for a rollback signal."""


class CheckpointRail(DeepAgentRail):
    """Checkpoints every step boundary and restores the context before a round."""

    # Lowest tier: a checkpoint records the state after every other rail ran.
    priority = 1

    def __init__(self, harness: "RecoverableHarness"):
        super().__init__()
        self._harness = harness
        self._deep_agent: "DeepAgent | None" = None

    def callback_priority(self, event: AgentCallbackEvent) -> int:
        if event == AgentCallbackEvent.BEFORE_INVOKE:
            return _RESTORE_PRIORITY
        if event in (AgentCallbackEvent.AFTER_TOOL_CALL, AgentCallbackEvent.ON_TOOL_EXCEPTION):
            return _OUTCOME_PRIORITY
        return self.priority

    @property
    def deep_agent(self) -> "DeepAgent | None":
        """The agent this rail is mounted on; its tools run undos."""
        return self._deep_agent

    def init(self, agent) -> None:
        self._deep_agent = agent
        self._harness.add_declaration_params(agent)

    def uninit(self, agent) -> None:
        if self._deep_agent is agent:
            self._deep_agent = None

    def set_workspace(self, workspace: "Workspace") -> None:
        super().set_workspace(workspace)
        self._bind()

    def set_sys_operation(self, sys_operation: "SysOperation") -> None:
        super().set_sys_operation(sys_operation)
        self._bind()

    def _bind(self) -> None:
        if self.workspace is None:
            return
        sandboxed = getattr(self.sys_operation, "mode", OperationMode.LOCAL) == OperationMode.SANDBOX
        root = Path(self.workspace.root_path).expanduser().resolve()
        kept = set()
        for node in _KEPT_NODES:
            path = self.workspace.get_node_path(node)
            if path is not None:
                resolved = Path(path).expanduser().resolve()
                if root in resolved.parents:
                    kept.add(resolved.relative_to(root).as_posix())
        self._harness.bind_workspace(self.workspace.root_path, sandboxed=sandboxed, kept_dirs=frozenset(kept))

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None or self._deep_agent is None:
            return
        self._harness.add_declaration_params(self._deep_agent)  # Tools may have been reloaded.
        await self._harness.begin_round(ctx.session, self._deep_agent)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        await self._take(ctx)
        self._stop_for_signal(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is not None:
            await self._harness.before_tool(ctx.session, ctx)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        await self._harness.after_tool(ctx, ctx.exception)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        await self._harness.after_tool(ctx, ctx.exception)

    async def after_react_iteration(self, ctx: AgentCallbackContext) -> None:
        await self._take(ctx)
        self._stop_for_signal(ctx)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None:
            return
        try:
            if ctx.exception is None:
                await self._take(ctx)
        finally:
            await self._harness.end_round(ctx.session)

    def _stop_for_signal(self, ctx: AgentCallbackContext) -> None:
        """End the round here when a rollback signal waits; the step is already checkpointed."""
        if ctx.session is None or not self._harness.waiting_signal(ctx.session.get_session_id()):
            return
        ctx.request_force_finish({
            "result_type": ROLLBACK_RESULT_TYPE,
            "output": "Stopped at a step boundary for a rollback request; its plan awaits a person's decision.",
        })

    async def _take(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None or self._deep_agent is None:
            return
        try:
            await self._harness.take_checkpoint(ctx.session, self._deep_agent)
        except Exception:  # A checkpoint failure must not stop the agent.
            logger.exception("recoverable: checkpoint failed for session %s", ctx.session.get_session_id())
