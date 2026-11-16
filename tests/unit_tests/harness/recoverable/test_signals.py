# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Rollback signals on a real DeepAgent: waiting for a step boundary, one open signal, limits, events."""
from __future__ import annotations

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.harness.recoverable import (
    ROLLBACK_RESULT_TYPE,
    EventKind,
    Initiator,
    RecoverableHarness,
    RollbackApproval,
    RollbackSignal,
    SignalStatus,
)
from tests.unit_tests.fixtures.deep_agent_interrupts import CountingTool, messages_seen, run, scripted
from tests.unit_tests.harness.recoverable.test_harness import (  # noqa: F401
    _agent,
    _contents,
    _model,
    _two_tool_round,
    runner,
)
from tests.unit_tests.fixtures.mock_llm import create_text_response


class SignalingTool(CountingTool):
    """Sends a rollback signal from inside its first call, as a monitor would while the agent runs."""

    def __init__(self, name: str, send):
        super().__init__(name)
        self._send = send
        self.ticket = None

    async def invoke(self, inputs, **kwargs):
        result = await super().invoke(inputs, **kwargs)
        if self.invoke_count == 1:
            self.ticket = await self._send()
        return result


def _signal(signal_id: str, target_id: str, initiator: Initiator = Initiator.HUMAN, **fields) -> RollbackSignal:
    return RollbackSignal(signal_id=signal_id, session_id="s1", target_id=target_id, initiator=initiator, **fields)


async def _checkpoints(harness) -> list[str]:
    """Checkpoint ids oldest first."""
    return [c.checkpoint_id for c in reversed(await harness.history("s1"))]


async def _ran_once(tmp_path, **options):
    events = []
    harness = RecoverableHarness(kv_store=InMemoryKVStore(), storage_dir=tmp_path / "recoverable",
                                 on_event=events.append, **options)
    agent = _agent(tmp_path, harness, CountingTool("work"))
    with scripted(_model(*_two_tool_round())):
        await run(agent, "do the work", "s1")
    return harness, agent, events


@pytest.mark.asyncio
async def test_a_signal_while_running_ends_the_round_at_the_next_step_boundary(tmp_path, runner):
    harness = RecoverableHarness(kv_store=InMemoryKVStore(), storage_dir=tmp_path / "recoverable")

    async def send():
        [c0] = await _checkpoints(harness)
        return await harness.request_rollback(_signal("jev-1", c0, Initiator.AUTO, source="JEV", reason="off track"))

    tool = SignalingTool("work", send)
    agent = _agent(tmp_path, harness, tool)
    llm = _model(*_two_tool_round(), create_text_response("second try done"))
    with scripted(llm):
        result = await run(agent, "do the work", "s1")

        assert tool.ticket.status == SignalStatus.WAITING
        assert result["result_type"] == ROLLBACK_RESULT_TYPE
        assert tool.invoke_count == 1, "the round ended at the boundary after the first step"
        c0, c1 = await _checkpoints(harness)  # Round start and the end of the step that ran.
        ticket = await harness.get_ticket("s1", "jev-1")
        assert ticket.status == SignalStatus.AWAITING_APPROVAL
        applied = await harness.apply_rollback(RollbackApproval(plan_id=ticket.plan_id, target_id=c0))
        assert applied.abandoned_ids == [c1]
        assert (await harness.get_ticket("s1", "jev-1")).status == SignalStatus.APPLIED

        await run(agent, "go on", "s1")

    note = next(text for text in _contents(messages_seen(llm)) if text.startswith("[ROLLBACK]"))
    assert "JEV rolled this conversation back" in note and "off track" in note


@pytest.mark.asyncio
async def test_one_signal_is_open_at_a_time_and_a_resent_signal_gets_its_ticket(tmp_path, runner):
    harness, _, _ = await _ran_once(tmp_path)
    c0, c1, _, _ = await _checkpoints(harness)

    first = await harness.request_rollback(_signal("a", c1))
    assert first.status == SignalStatus.AWAITING_APPROVAL and first.plan_id is not None
    assert await harness.request_rollback(_signal("a", c1)) == first
    other = await harness.request_rollback(_signal("b", c0))
    assert other.status == SignalStatus.REJECTED and "still open" in other.detail

    await harness.decline_rollback(first.plan_id, "not now")
    declined = await harness.get_ticket("s1", "a")
    assert declined.status == SignalStatus.INVALIDATED and "not now" in declined.detail
    with pytest.raises(BaseError):
        await harness.apply_rollback(RollbackApproval(plan_id=first.plan_id, target_id=c1))
    assert len(await harness.history("s1")) == 4, "a declined plan changes nothing"

    assert (await harness.request_rollback(_signal("c", c0))).status == SignalStatus.AWAITING_APPROVAL
    gone = await harness.request_rollback(_signal("d", "no-such-checkpoint"))
    assert gone.status == SignalStatus.REJECTED and "not on the history" in gone.detail


@pytest.mark.asyncio
async def test_a_persons_signal_replaces_an_open_automatic_one(tmp_path, runner):
    harness, _, _ = await _ran_once(tmp_path)
    c0, c1, _, _ = await _checkpoints(harness)

    auto = await harness.request_rollback(_signal("jev", c1, Initiator.AUTO))
    person = await harness.request_rollback(_signal("me", c0))

    assert person.status == SignalStatus.AWAITING_APPROVAL
    replaced = await harness.get_ticket("s1", "jev")
    assert replaced.status == SignalStatus.INVALIDATED and "superseded" in replaced.detail
    with pytest.raises(BaseError):
        await harness.apply_rollback(RollbackApproval(plan_id=auto.plan_id, target_id=c1))
    blocked = await harness.request_rollback(_signal("jev-2", c1, Initiator.AUTO))
    assert blocked.status == SignalStatus.REJECTED, "an automatic signal never replaces a person's"


@pytest.mark.asyncio
async def test_automatic_signals_are_limited_per_target_and_per_round(tmp_path, runner):
    harness, agent, _ = await _ran_once(tmp_path, auto_limit_per_target=2, auto_limit_per_round=3)
    c0, c1, c2, c3 = await _checkpoints(harness)

    async def auto(signal_id: str, target: str):
        ticket = await harness.request_rollback(_signal(signal_id, target, Initiator.AUTO))
        if ticket.status == SignalStatus.AWAITING_APPROVAL:
            await harness.decline_rollback(ticket.plan_id)
        return ticket

    assert (await auto("1", c3)).status == SignalStatus.AWAITING_APPROVAL
    assert (await auto("2", c3)).status == SignalStatus.AWAITING_APPROVAL
    over_target = await auto("3", c3)
    assert over_target.status == SignalStatus.REJECTED and "limit of 2" in over_target.detail
    assert (await auto("4", c2)).status == SignalStatus.AWAITING_APPROVAL
    over_round = await auto("5", c1)
    assert over_round.status == SignalStatus.REJECTED and "this round" in over_round.detail

    with scripted(_model(create_text_response("next request"))):
        await run(agent, "something else", "s1")
    assert (await auto("6", c1)).status == SignalStatus.AWAITING_APPROVAL, "a new round starts a new count"


@pytest.mark.asyncio
async def test_a_round_that_writes_back_a_rollback_keeps_the_automatic_count(tmp_path, runner):
    harness, agent, _ = await _ran_once(tmp_path, auto_limit_per_round=2)
    c0, c1, c2, _ = await _checkpoints(harness)

    ticket = await harness.request_rollback(_signal("1", c2, Initiator.AUTO))
    await harness.apply_rollback(RollbackApproval(plan_id=ticket.plan_id, target_id=c2))
    with scripted(_model(create_text_response("continuing"))):
        await run(agent, "continue", "s1")

    second = await harness.request_rollback(_signal("2", c1, Initiator.AUTO))
    assert second.status == SignalStatus.AWAITING_APPROVAL
    await harness.decline_rollback(second.plan_id)
    third = await harness.request_rollback(_signal("3", c0, Initiator.AUTO))
    assert third.status == SignalStatus.REJECTED and "this round" in third.detail


@pytest.mark.asyncio
async def test_checkpoints_and_rollbacks_are_announced(tmp_path, runner):
    harness, _, events = await _ran_once(tmp_path)
    c0, c1, _, _ = await _checkpoints(harness)
    created = [e for e in events if e.kind == EventKind.CHECKPOINT_CREATED]
    assert [(e.checkpoint_id, e.seq, e.round) for e in created[:2]] == [(c0, 0, 1), (c1, 1, 1)]
    assert len(created) == 4

    events.clear()
    ticket = await harness.request_rollback(_signal("a", c1, source="IDE"))
    await harness.apply_rollback(RollbackApproval(plan_id=ticket.plan_id, target_id=c1))
    await harness.request_rollback(_signal("b", "gone"))

    assert [(e.kind, e.signal_id) for e in events] == [
        (EventKind.ROLLBACK_RECEIVED, "a"),
        (EventKind.ROLLBACK_AWAITING_APPROVAL, "a"),
        (EventKind.ROLLBACK_APPLIED, "a"),
        (EventKind.ROLLBACK_RECEIVED, "b"),
        (EventKind.ROLLBACK_REJECTED, "b"),
    ]
    assert events[2].plan_id == ticket.plan_id and events[2].checkpoint_id == c1
