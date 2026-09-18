# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Default-off state remains unchanged; opted-in Sessions cannot contaminate it."""

from unittest.mock import patch

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.context_engine import (
    ContextEngine,
    ContextEngineConfig,
    CycleArchiveProcessorConfig,
    SessionHistoryConfig,
)
from openjiuwen.core.context_engine.processor.budget_guard import guard_history_window
from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard
from tests.unit_tests.core.context_engine.test_cycle_archive_processor import make_context, make_engine


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [None, SessionHistoryConfig(enabled=False, root_dir="unused")])
async def test_disabled_has_no_new_work_or_checkpoint_keys(history):
    engine = ContextEngine(ContextEngineConfig(session_history=history))
    with (
        patch("openjiuwen.core.context_engine.context_engine.SessionHistoryRecorder", side_effect=AssertionError),
        patch(
            "openjiuwen.core.context_engine.processor.budget_guard.history_window_tokens",
            side_effect=AssertionError,
        ),
    ):
        context = await engine.create_context()
        await context.add_messages(UserMessage(content="original behavior"))
        window = await context.get_context_window()
        guard_history_window(context, window)
        assert set(context.save_state()) == {"messages", "offload_messages", "last_context_window_access_at"}
        assert window.context_messages == context.get_messages()
        assert engine._history_recorders is None


@pytest.mark.asyncio
async def test_new_failure_does_not_affect_old_instance(tmp_path):
    new = make_engine(tmp_path)
    _, new_context = await make_context(new)
    old = ContextEngine()
    old_context = await old.create_context()
    await old_context.add_messages(UserMessage(content="keep old behavior"))
    await new_context.add_messages([AssistantMessage(content="x" * 6000), AssistantMessage(content="latest")])
    with patch("openjiuwen.core.context_engine.history.store.os.link", side_effect=OSError("failed")):
        window = await new_context.get_context_window()
    with pytest.raises(BaseError, match="ARCHIVE_FAILED"):
        guard_history_window(new_context, window)
    guard_history_window(old_context, await old_context.get_context_window())
    assert old_context.get_messages()[0].content == "keep old behavior"
    assert "session_history" not in old_context.save_state()


@pytest.mark.asyncio
async def test_legacy_snapshot_unknown_time_and_session_key_isolation(tmp_path):
    engine = make_engine(tmp_path)
    one = create_agent_session(session_id="a_b", card=AgentCard(id="one"))
    two = create_agent_session(session_id="a", card=AgentCard(id="two"))
    one.update_state({"context": {"c": {"messages": [UserMessage(content="legacy")], "offload_messages": {}}}})
    for session in [one, two]:
        engine.begin_history_execution(session)
    processors = [("CycleArchiveProcessor", CycleArchiveProcessorConfig())]
    first = await engine.create_context("c", one, processors=processors)
    second = await engine.create_context("b_c", two, processors=processors)
    assert first is not second
    assert second.get_messages() == []
    record = first._session_history.originals(first.get_messages())[0]
    assert record.occurred_at is None and record.execution_id == "legacy"
    assert first._session_history.current_records() == []


def test_new_config_cannot_silently_drop_history_or_hot_switch(tmp_path):
    history = SessionHistoryConfig(
        enabled=True, root_dir=str(tmp_path), output_reserve_tokens=0, safety_margin_tokens=0
    )
    for conflict in ({"default_window_message_num": 10}, {"max_context_message_num": 5}, {"enable_reload": True}):
        with pytest.raises(ValueError):
            ContextEngineConfig(context_window_tokens=1000, session_history=history, **conflict)
    engine = make_engine(tmp_path)
    with pytest.raises(BaseError, match="cannot be changed"):
        engine.rebind_context_model(ContextEngineConfig())
    assert engine.history_enabled
