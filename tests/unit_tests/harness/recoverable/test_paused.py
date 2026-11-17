# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A rollback while a tool call waits for confirmation.

The round paused on ``work``, which the permission rail asks about. Any rollback
discards that unfinished step, so the call must never run afterwards, and a
late answer to it (an old confirmation card clicked in a host UI) is refused.
"""
from __future__ import annotations

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.harness.recoverable import RollbackApproval
from tests.unit_tests.fixtures.deep_agent_interrupts import (
    APPROVE,
    CountingTool,
    ask_permissions,
    by_call_id,
    by_interrupt_id,
    make_deep_agent,
    messages_seen,
    pending,
    run,
    scripted,
)
from tests.unit_tests.fixtures.mock_llm import create_text_response, create_tool_call_response
from tests.unit_tests.harness.recoverable.test_harness import _contents, _harness, _model, runner  # noqa: F401

REFUSED = r"discarded the calls waiting for confirmation \(work\); this answer is refused"


def _setup(tmp_path):
    harness = _harness(tmp_path)
    work = CountingTool("work")
    agent = make_deep_agent(tmp_path / "ws", tools=[work, CountingTool("free")], rails=[harness.rail()],
                            name="Paused", permissions=ask_permissions("work"))
    return harness, agent, work


def _paused_round() -> list:
    return [
        create_tool_call_response("free", '{"x": "0"}', tool_call_id="call_0"),
        create_tool_call_response("work", '{"x": "1"}', tool_call_id="call_1"),
    ]


async def _roll_back(harness, checkpoint):
    plan = await harness.plan_rollback("s1", checkpoint.checkpoint_id)
    await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=checkpoint.checkpoint_id))
    return plan


@pytest.mark.asyncio
async def test_the_plan_lists_the_call_it_cancels_and_a_late_answer_is_refused(tmp_path, runner):
    harness, agent, work = _setup(tmp_path)
    with scripted(_model(*_paused_round())):
        [(call_id, request)] = pending(await run(agent, "do the work", "s1"))
        head = (await harness.history("s1"))[0]

        plan = await _roll_back(harness, head)
        assert [(c.tool_name, c.tool_call_id) for c in plan.cancelled_calls] == [("work", call_id)]

        for answer in (by_call_id(call_id, APPROVE), by_interrupt_id(request, APPROVE)):
            with pytest.raises(BaseError, match=REFUSED):
                await run(agent, answer, "s1")

    assert work.invoke_count == 0, "the discarded call must not run"
    assert [c.checkpoint_id for c in await harness.history("s1")][0] == head.checkpoint_id, (
        "a refused answer changes nothing; the rollback is written back by the next round"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["head", "round_start"])
async def test_a_new_message_after_the_rollback_starts_a_clean_round(tmp_path, runner, target):
    harness, agent, work = _setup(tmp_path)
    llm = _model(*_paused_round(), create_text_response("doing something else"))
    with scripted(llm):
        [(call_id, _)] = pending(await run(agent, "do the work", "s1"))
        history = await harness.history("s1")
        await _roll_back(harness, history[0] if target == "head" else history[-1])

        result = await run(agent, "do something else", "s1")

    assert result["result_type"] == "answer", "the new message is not taken as the answer to the old call"
    assert work.invoke_count == 0
    seen = messages_seen(llm)
    assert not any(call_id in str(m) for m in seen), "no trace of the discarded call reaches the model"
    texts = _contents(seen)
    note = next(i for i, text in enumerate(texts) if text.startswith("[ROLLBACK]"))
    assert texts[note + 1] == "do something else"
    assert (await harness.history("s1"))[0].label == "reply: doing something else"


@pytest.mark.asyncio
async def test_a_late_answer_is_refused_rounds_later_but_a_new_pause_can_be_answered(tmp_path, runner):
    harness, agent, work = _setup(tmp_path)
    llm = _model(
        *_paused_round(),
        create_text_response("ok"),
        # The model asks for the same tool again; a scripted model reuses the call id.
        create_tool_call_response("work", '{"x": "2"}', tool_call_id="call_1"),
        create_text_response("done"),
    )
    with scripted(llm):
        [(call_id, _)] = pending(await run(agent, "do the work", "s1"))
        await _roll_back(harness, (await harness.history("s1"))[0])
        await run(agent, "never mind", "s1")

        with pytest.raises(BaseError, match=REFUSED):
            await run(agent, by_call_id(call_id, APPROVE), "s1")
        assert work.invoke_count == 0

        [(new_call_id, _)] = pending(await run(agent, "now do the work", "s1"))
        assert (await harness.plan_rollback("s1", (await harness.history("s1"))[0].checkpoint_id)).cancelled_calls
        await run(agent, by_call_id(new_call_id, APPROVE), "s1")

    assert work.invoke_count == 1, "an answer to the call waiting now is not mistaken for the discarded one"
    plan = await harness.plan_rollback("s1", (await harness.history("s1"))[0].checkpoint_id)
    assert plan.cancelled_calls == [], "nothing waits once the round finished"
