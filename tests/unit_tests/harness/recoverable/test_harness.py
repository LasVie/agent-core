# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Recoverable Harness checkpoints and context rollback on a real DeepAgent.

The model is scripted; the DeepAgent, its rails, the ReAct loop, the session and
vcs are real. Rollbacks are planned and applied through the public harness API.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.harness.recoverable import (
    CheckpointKind,
    RecoverableHarness,
    RollbackApproval,
)
from tests.unit_tests.fixtures.deep_agent_interrupts import (
    APPROVE,
    CountingTool,
    ask_permissions,
    by_call_id,
    make_deep_agent,
    messages_seen,
    pending,
    run,
    scripted,
)
from tests.unit_tests.fixtures.mock_llm import MockLLMModel, create_text_response, create_tool_call_response


@pytest_asyncio.fixture
async def runner():
    await Runner.start()
    try:
        yield
    finally:
        await Runner.stop()


def _harness(tmp_path, kv_store=None) -> RecoverableHarness:
    return RecoverableHarness(kv_store=kv_store or InMemoryKVStore(), storage_dir=tmp_path / "recoverable")


def _agent(tmp_path, harness: RecoverableHarness, tool: CountingTool, **kwargs):
    return make_deep_agent(tmp_path / "ws", tools=[tool], rails=[harness.rail()], name="Recoverable", **kwargs)


def _model(*responses) -> MockLLMModel:
    llm = MockLLMModel()
    llm.set_responses(list(responses))
    return llm


def _contents(messages) -> list[str]:
    return [str(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in messages]


def _two_tool_round() -> list:
    return [
        create_tool_call_response("work", '{"x": "1"}', tool_call_id="call_1"),
        create_tool_call_response("work", '{"x": "2"}', tool_call_id="call_2"),
        create_text_response("round one done"),
    ]


@pytest.mark.asyncio
async def test_checkpoint_at_every_step_boundary(tmp_path, runner):
    harness = _harness(tmp_path)
    agent = _agent(tmp_path, harness, CountingTool("work"))
    with scripted(_model(*_two_tool_round())):
        await run(agent, "do the work", "s1")

    history = list(reversed(await harness.history("s1")))
    # Round start, two step ends, round end. Model calls that changed nothing reuse a checkpoint.
    assert [c.seq for c in history] == [0, 1, 2, 3]
    assert all(c.kind == CheckpointKind.STEP for c in history)
    assert [c.parent_id for c in history[1:]] == [c.checkpoint_id for c in history[:-1]]
    assert len({c.event_id for c in history}) == 4


@pytest.mark.asyncio
async def test_rollback_restores_context_and_tells_the_agent(tmp_path, runner):
    harness = _harness(tmp_path)
    tool = CountingTool("work")
    agent = _agent(tmp_path, harness, tool)
    llm = _model(*_two_tool_round(), create_text_response("round two done"))
    with scripted(llm):
        await run(agent, "do the work", "s1")
        c0, c1, c2, c3 = reversed(await harness.history("s1"))

        plan = await harness.plan_rollback("s1", c1.checkpoint_id, reason="second step was wrong")
        assert plan.abandoned_ids == [c3.checkpoint_id, c2.checkpoint_id]
        result = await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=c1.checkpoint_id))
        assert result.abandoned_ids == plan.abandoned_ids
        assert [c.checkpoint_id for c in await harness.history("s1")] == [c1.checkpoint_id, c0.checkpoint_id]

        await run(agent, "try again", "s1")

    seen = _contents(messages_seen(llm))
    assert any("do the work" in text for text in seen)
    assert not any("round one done" in text for text in seen), "the discarded round end must be gone"
    assert sum("call_2" in str(m) for m in messages_seen(llm)) == 0, "the discarded second step must be gone"
    note = next(i for i, text in enumerate(seen) if text.startswith("[ROLLBACK]"))
    assert "checkpoint #1" in seen[note] and "second step was wrong" in seen[note]
    assert seen.index(next(t for t in seen if "try again" in t)) > note

    history = await harness.history("s1")
    rollback = next(c for c in history if c.kind == CheckpointKind.ROLLBACK)
    assert rollback.parent_id == c1.checkpoint_id and rollback.seq == 2
    assert history[-2:] == [c1, c0]
    for gone in (c2, c3):
        with pytest.raises(BaseError):
            await harness.plan_rollback("s1", gone.checkpoint_id)


@pytest.mark.asyncio
async def test_rollback_needs_a_matching_unapplied_approval(tmp_path, runner):
    harness = _harness(tmp_path)
    agent = _agent(tmp_path, harness, CountingTool("work"))
    with scripted(_model(*_two_tool_round())):
        await run(agent, "do the work", "s1")
    c0, c1 = list(reversed(await harness.history("s1")))[:2]

    plan = await harness.plan_rollback("s1", c1.checkpoint_id)
    with pytest.raises(BaseError):
        await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=c0.checkpoint_id))
    assert len(await harness.history("s1")) == 4, "a mismatched approval changes nothing"

    await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=c1.checkpoint_id))
    with pytest.raises(BaseError):
        await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=c1.checkpoint_id))


@pytest.mark.asyncio
async def test_no_checkpoint_while_a_tool_call_waits_for_confirmation(tmp_path, runner):
    harness = _harness(tmp_path)
    tool = CountingTool("work")
    agent = _agent(tmp_path, harness, tool, permissions=ask_permissions("work"))
    llm = _model(create_tool_call_response("work", '{"x": "1"}', tool_call_id="call_1"), create_text_response("ok"))
    with scripted(llm):
        [(call_id, _)] = pending(await run(agent, "do the work", "s1"))
        assert [c.seq for c in await harness.history("s1")] == [0], "the unfinished step has no checkpoint"

        await run(agent, by_call_id(call_id, APPROVE), "s1")

    assert tool.invoke_count == 1
    history = list(reversed(await harness.history("s1")))
    assert [c.seq for c in history] == [0, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["default_sqlite", "injected_store"])
async def test_session_is_restored_after_a_restart(tmp_path, runner, storage):
    kv_store = InMemoryKVStore()

    def make_harness() -> RecoverableHarness:
        if storage == "default_sqlite":
            return RecoverableHarness(storage_dir=tmp_path / "recoverable")
        return _harness(tmp_path, kv_store)

    first = make_harness()
    with scripted(_model(*_two_tool_round())):
        await run(_agent(tmp_path, first, CountingTool("work")), "do the work", "s1")

    # A new process: new harness and agent over the same storage, and an empty session.
    second = make_harness()
    agent = _agent(tmp_path, second, CountingTool("work"))
    llm = _model(create_text_response("continued"))
    with scripted(llm):
        session = create_agent_session(session_id="s1", card=agent.card)
        await agent.invoke({"query": "continue", "conversation_id": "s1"}, session=session)

    seen = _contents(messages_seen(llm))
    assert any("do the work" in text for text in seen) and any("round one done" in text for text in seen)
    assert "continue" in seen[-1]
    # The history carries on from the first process: round start and round end of the new round.
    assert [c.seq for c in reversed(await second.history("s1"))] == [0, 1, 2, 3, 4, 5]


def test_storage_inside_the_workspace_is_refused(tmp_path):
    harness = RecoverableHarness(kv_store=InMemoryKVStore(), storage_dir=tmp_path / "ws" / ".recoverable")
    with pytest.raises(BaseError):
        harness.check_workspace(tmp_path / "ws")
    harness.check_workspace(tmp_path / "elsewhere")
