# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""No provider call or retry when strict history admission fails."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.context_engine import ContextEngineConfig, CycleArchiveProcessorConfig, SessionHistoryConfig
from openjiuwen.core.foundation.llm import AssistantMessage, BaseMessage, SystemMessage, UserMessage
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail, InvokeInputs
from openjiuwen.core.single_agent.schema.agent_card import AgentCard


class RetryEverything(AgentRail):
    async def on_model_exception(self, ctx):
        raise AssertionError("local history admission must bypass model retries")


@pytest_asyncio.fixture(autouse=True)
async def runner():
    await Runner.start()
    yield
    await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", ["archive", "budget", "attachment"])
async def test_final_guard_blocks_provider(tmp_path, streaming, failure):
    card = AgentCard(id="guard-test")
    agent = ReActAgent(card=card)
    config = ReActAgentConfig(
        context_engine_config=ContextEngineConfig(
            context_window_tokens=600,
            session_history=SessionHistoryConfig(
                enabled=True, root_dir=str(tmp_path), output_reserve_tokens=0, safety_margin_tokens=0
            ),
        )
    )
    config.configure_model_client(provider="OpenAI", api_key="fake", api_base="http://localhost:1", model_name="test")
    agent.configure(config)
    await agent.register_rail(RetryEverything())
    session = create_agent_session(session_id="s", card=card)
    agent.context_engine.begin_history_execution(session)
    context = await agent.context_engine.create_context(
        session=session,
        processors=[("CycleArchiveProcessor", CycleArchiveProcessorConfig())],
    )
    messages: list[BaseMessage] = (
        [AssistantMessage(content="x" * 6000), AssistantMessage(content="latest")]
        if failure == "archive"
        else [UserMessage(content="x" * 6000 if failure == "budget" else "short")]
    )
    await context.add_messages(messages)
    if failure == "attachment":

        async def attach(_context, window):
            window.system_messages.append(SystemMessage(content="attachment" * 1000))
            return window

        agent.context_engine.register_window_mutator(attach)
    provider = MagicMock()
    provider.invoke = AsyncMock()
    provider.stream = MagicMock()
    provider.model_client_config = None
    ctx = AgentCallbackContext(agent=agent, inputs=InvokeInputs(query="test"), session=session, context=context)
    ctx.extra["_streaming"] = streaming
    with (
        patch.object(agent, "_get_llm", return_value=provider),
        patch(
            "openjiuwen.core.context_engine.history.store.os.link",
            side_effect=OSError("write failed"),
        ),
    ):
        with pytest.raises(BaseError, match="ARCHIVE_FAILED" if failure == "archive" else "CONTEXT_BUDGET_EXCEEDED"):
            await agent._call_model(ctx, context, [])
    provider.invoke.assert_not_called()
    provider.stream.assert_not_called()
