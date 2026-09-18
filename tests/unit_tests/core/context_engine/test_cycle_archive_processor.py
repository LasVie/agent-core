# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from unittest.mock import patch

import anyio
import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.context_engine import (
    ContextEngine,
    ContextEngineConfig,
    CycleArchiveProcessorConfig,
    SessionHistoryConfig,
)
from openjiuwen.core.context_engine.processor.budget_guard import guard_history_window, history_window_tokens
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage, UserMessage
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard


def make_engine(tmp_path, budget=600):
    return ContextEngine(
        ContextEngineConfig(
            context_window_tokens=budget,
            session_history=SessionHistoryConfig(
                enabled=True, root_dir=str(tmp_path), output_reserve_tokens=0, safety_margin_tokens=0
            ),
        )
    )


async def make_context(engine, session_id="s"):
    session = create_agent_session(session_id=session_id, card=AgentCard(id="test"))
    engine.begin_history_execution(session)
    context = await engine.create_context(
        session=session, processors=[("CycleArchiveProcessor", CycleArchiveProcessorConfig())]
    )
    return session, context


@pytest.mark.asyncio
async def test_minimal_cycles_and_snapshot_restore(tmp_path):
    engine = make_engine(tmp_path)
    session, context = await make_context(engine)
    user = UserMessage(content="current")
    messages = [
        user,
        AssistantMessage(content="a" * 900),
        AssistantMessage(content="b" * 900),
        AssistantMessage(content="latest"),
    ]
    await context.add_messages(messages)
    window = await context.get_context_window()
    guard_history_window(context, window)
    assert history_window_tokens(context, window) <= 600
    assert user in context.get_messages()
    assert messages[1] not in context.get_messages()
    assert messages[2] in context.get_messages()  # Remove the minimum, not all old cycles.
    assert context.get_messages() == window.context_messages
    result = await engine.finish_history_execution(session)
    assert len((await anyio.Path(result.trajectory.path).read_text("utf-8")).splitlines()) == 4
    restarted = make_engine(tmp_path)
    restarted.begin_history_execution(session)
    restored = await restarted.create_context(
        session=session, processors=[("CycleArchiveProcessor", CycleArchiveProcessorConfig())]
    )
    assert restored.get_messages() == context.get_messages()
    assert restored._session_history.current_records() == []


@pytest.mark.asyncio
async def test_write_failure_keeps_originals_and_final_guard_fails(tmp_path):
    engine = make_engine(tmp_path)
    _, context = await make_context(engine)
    await context.add_messages([AssistantMessage(content="a" * 3000), AssistantMessage(content="latest")])
    before = context.get_messages()
    with patch("openjiuwen.core.context_engine.history.store.os.link", side_effect=OSError("disk full")):
        window = await context.get_context_window()
    assert context.get_messages() == before
    with pytest.raises(BaseError, match="ARCHIVE_FAILED"):
        guard_history_window(context, window)
    assert not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.asyncio
async def test_giant_latest_tool_saved_whole_with_pairing(tmp_path):
    engine = make_engine(tmp_path)
    session, context = await make_context(engine)
    assistant = AssistantMessage(
        content="", tool_calls=[ToolCall(id="t", type="function", name="read", arguments="{}")]
    )
    tool = ToolMessage(tool_call_id="t", content="original" * 1500)
    await context.add_messages([UserMessage(content="question"), assistant, tool])
    window = await context.get_context_window()
    guard_history_window(context, window)
    assert window.context_messages[-1].tool_call_id == "t"
    assert "OFFLOAD" in window.context_messages[-1].content
    archive = next(tmp_path.rglob("offload/*.jsonl"))
    assert tool.content in archive.read_text("utf-8")
    result = await engine.finish_history_execution(session)
    assert tool.content in (await anyio.Path(result.trajectory.path).read_text("utf-8"))


@pytest.mark.asyncio
async def test_below_budget_creates_no_files_and_protected_overflow(tmp_path):
    engine = make_engine(tmp_path)
    _, context = await make_context(engine)
    await context.add_messages(UserMessage(content="short"))
    guard_history_window(context, await context.get_context_window())
    assert not list(tmp_path.iterdir())
    await context.add_messages(UserMessage(content="x" * 6000))
    with pytest.raises(BaseError, match="CONTEXT_BUDGET_EXCEEDED"):
        guard_history_window(context, await context.get_context_window())
