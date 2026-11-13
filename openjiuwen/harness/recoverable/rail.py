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
  tool changes a file before its original content is stored.
- ``AFTER_REACT_ITERATION`` (inner ReAct): checkpoint the step end.
- ``AFTER_INVOKE`` (outer DeepAgent): checkpoint the round end, unless the round
  raised; mark the session idle.

A failed checkpoint is logged and never interrupts the agent; that step just
cannot be a rollback target.

The workspace and sys_operation are injected before ``init``. A sandboxed
sys_operation means the workspace is not on this machine, so files are not
snapshotted.
"""

from __future__ import annotations

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
        return self.priority

    def init(self, agent) -> None:
        self._deep_agent = agent

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
        self._harness.bind_workspace(self.workspace.root_path, sandboxed=sandboxed)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None or self._deep_agent is None:
            return
        await self._harness.begin_round(ctx.session, self._deep_agent)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        await self._take(ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is not None:
            await self._harness.before_tool(ctx.session)

    async def after_react_iteration(self, ctx: AgentCallbackContext) -> None:
        await self._take(ctx)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None:
            return
        try:
            if ctx.exception is None:
                await self._take(ctx)
        finally:
            await self._harness.end_round(ctx.session)

    async def _take(self, ctx: AgentCallbackContext) -> None:
        if ctx.session is None or self._deep_agent is None:
            return
        try:
            await self._harness.take_checkpoint(ctx.session, self._deep_agent)
        except Exception:  # A checkpoint failure must not stop the agent.
            logger.exception("recoverable: checkpoint failed for session %s", ctx.session.get_session_id())
