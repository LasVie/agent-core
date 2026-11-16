# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Side-effect ledger and undo on a real DeepAgent.

A fake ticket service stands in for an external system: ``create_ticket`` is
reversible through ``close_ticket``, ``send_message`` is not declared and so
counts as irreversible.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.harness.recoverable import (
    EffectAction,
    EffectCategory,
    EffectDeclaration,
    EffectOutcome,
    RecoverableHarness,
    RollbackApproval,
    RollbackStatus,
)
from openjiuwen.harness.recoverable.harness import RecoverableHarness as _Harness
from openjiuwen.harness.recoverable.store import CheckpointStore
from tests.unit_tests.fixtures.deep_agent_interrupts import make_deep_agent, messages_seen, run, scripted
from tests.unit_tests.fixtures.mock_llm import MockLLMModel, create_text_response, create_tool_call_response


@pytest_asyncio.fixture
async def runner():
    await Runner.start()
    try:
        yield
    finally:
        await Runner.stop()


class TicketService:
    def __init__(self):
        self.open: dict[str, str] = {}
        self.closed: list[str] = []
        self.sent: list[str] = []
        self.created = 0
        self.close_failures = 0
        self.omit_id = False


def _card(name: str, **properties) -> ToolCard:
    return ToolCard(id=name, name=name, description=name,
                    input_params={"type": "object", "properties": properties})


class CreateTicket(Tool):
    def __init__(self, service: TicketService):
        super().__init__(_card("create_ticket", title={"type": "string"}))
        self.service = service

    async def invoke(self, inputs, **kwargs):
        self.service.created += 1
        ticket = f"T-{self.service.created}"
        self.service.open[ticket] = inputs.get("title", "")
        return {} if self.service.omit_id else {"id": ticket}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class CloseTicket(Tool):
    def __init__(self, service: TicketService):
        super().__init__(_card("close_ticket", ticket_id={"type": "string"}))
        self.service = service

    async def invoke(self, inputs, **kwargs):
        if self.service.close_failures:
            self.service.close_failures -= 1
            return {"success": False, "error": "service unavailable"}
        self.service.open.pop(inputs["ticket_id"])
        self.service.closed.append(inputs["ticket_id"])
        return {"success": True}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class SendMessage(Tool):
    def __init__(self, service: TicketService):
        super().__init__(_card("send_message", text={"type": "string"}))
        self.service = service

    async def invoke(self, inputs, **kwargs):
        self.service.sent.append(inputs.get("text", ""))
        return {"sent": True}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class WriteFile(Tool):
    def __init__(self, root: Path):
        super().__init__(_card("put", path={"type": "string"}, content={"type": "string"}))
        self.root = root

    async def invoke(self, inputs, **kwargs):
        path = self.root / inputs["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inputs["content"])
        return {"ok": True}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


DECLARATIONS = {
    "create_ticket": EffectDeclaration(
        category=EffectCategory.REVERSIBLE, undo_tool="close_ticket", undo_args={"ticket_id": "result.id"},
    ),
    "close_ticket": EffectDeclaration(category=EffectCategory.NONE),
    "put": EffectDeclaration(category=EffectCategory.NONE),
}


def _setup(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    service = TicketService()
    harness = RecoverableHarness(
        kv_store=InMemoryKVStore(), storage_dir=tmp_path / "recoverable", declarations=DECLARATIONS,
    )
    tools = [CreateTicket(service), CloseTicket(service), SendMessage(service), WriteFile(ws)]
    agent = make_deep_agent(ws, tools=tools, rails=[harness.rail()], name="RecoverableLedger")
    return ws, service, harness, agent


def _call(name: str, call_id: str, **args):
    return create_tool_call_response(name, json.dumps(args), tool_call_id=call_id)


def _model(*responses) -> MockLLMModel:
    llm = MockLLMModel()
    llm.set_responses(list(responses))
    return llm


def _three_effects() -> list:
    return [
        _call("create_ticket", "c1", title="a"),
        _call("create_ticket", "c2", title="b"),
        _call("send_message", "c3", text="progress"),
        create_text_response("done"),
    ]


async def _plan(harness, seq: int = 0):
    target = next(c for c in await harness.history("s1") if c.seq == seq)
    return await harness.plan_rollback("s1", target.checkpoint_id)


def _contents(llm) -> list[str]:
    return [str(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in messages_seen(llm)]


@pytest.mark.asyncio
async def test_reversible_effects_are_undone_newest_first(tmp_path, runner):
    _, service, harness, agent = _setup(tmp_path)
    llm = _model(*_three_effects(), create_text_response("again"))
    with scripted(llm):
        await run(agent, "file two tickets", "s1")
        assert sorted(service.open) == ["T-1", "T-2"]
        plan = await _plan(harness)
        assert [(e.tool_name, e.action) for e in plan.effects] == [
            ("send_message", EffectAction.IRREVERSIBLE),
            ("create_ticket", EffectAction.UNDO),
            ("create_ticket", EffectAction.UNDO),
        ]
        assert [e.undo_args for e in plan.effects[1:]] == [{"ticket_id": "T-2"}, {"ticket_id": "T-1"}]

        result = await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=plan.target_id))
        await run(agent, "try again", "s1")

    assert result.status == RollbackStatus.COMPLETED
    assert service.closed == ["T-2", "T-1"] and service.open == {}
    assert service.sent == ["progress"], "an irreversible effect stays"
    note = next(text for text in _contents(llm) if text.startswith("[ROLLBACK]"))
    assert "Undone outside the workspace: create_ticket" in note
    assert "Could NOT be undone and still in effect: send_message" in note


@pytest.mark.asyncio
async def test_an_undo_struck_from_the_plan_is_left_in_place(tmp_path, runner):
    _, service, harness, agent = _setup(tmp_path)
    with scripted(_model(*_three_effects())):
        await run(agent, "file two tickets", "s1")
        plan = await _plan(harness)
        first = plan.effects[2]  # T-1, the oldest
        result = await harness.apply_rollback(RollbackApproval(
            plan_id=plan.plan_id, target_id=plan.target_id, cancelled_effect_ids=[first.record_id],
        ))

    assert result.status == RollbackStatus.COMPLETED, "a cancelled undo is not a failure"
    assert service.open == {"T-1": "a"} and service.closed == ["T-2"]
    assert [e.outcome for e in result.effects if e.action == EffectAction.UNDO] == [
        EffectOutcome.UNDONE, EffectOutcome.CANCELLED,
    ]


@pytest.mark.asyncio
async def test_a_failed_undo_stops_the_rest_and_resume_finishes(tmp_path, runner):
    ws, service, harness, agent = _setup(tmp_path)
    service.close_failures = 1
    with scripted(_model(_call("put", "c0", path="a.txt", content="changed"), *_three_effects())):
        await run(agent, "file two tickets", "s1")
        plan = await _plan(harness)
        result = await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=plan.target_id))

    assert result.status == RollbackStatus.PARTIAL
    assert [e.outcome for e in result.effects if e.action == EffectAction.UNDO] == [
        EffectOutcome.FAILED, EffectOutcome.PENDING,
    ]
    assert sorted(service.open) == ["T-1", "T-2"], "the oldest undo was not attempted after the failure"
    assert not (ws / "a.txt").exists(), "the workspace is restored even when an undo fails"

    resumed = await harness.resume_rollback(plan.plan_id)
    assert resumed.status == RollbackStatus.COMPLETED
    assert service.closed == ["T-2", "T-1"] and service.open == {}
    with pytest.raises(Exception):
        await harness.resume_rollback(plan.plan_id)


@pytest.mark.asyncio
async def test_a_partial_rollback_can_be_accepted(tmp_path, runner):
    _, service, harness, agent = _setup(tmp_path)
    service.close_failures = 1
    with scripted(_model(*_three_effects())):
        await run(agent, "file two tickets", "s1")
        plan = await _plan(harness)
        await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=plan.target_id))

    accepted = await harness.accept_partial(plan.plan_id)
    assert accepted.status == RollbackStatus.ACCEPTED
    assert sorted(service.open) == ["T-1", "T-2"]


@pytest.mark.asyncio
async def test_a_call_whose_intent_cannot_be_recorded_does_not_run(tmp_path, runner):
    _, service, harness, agent = _setup(tmp_path)
    llm = _model(_call("create_ticket", "c1", title="a"), create_text_response("done"))

    async def broken(self, session_id):
        raise OSError("disk full")

    with scripted(llm), patch.object(CheckpointStore, "next_ledger_seq", broken):
        await run(agent, "file a ticket", "s1")

    assert service.created == 0, "an unrecorded call must not run"
    assert any("could not be recorded" in text for text in _contents(llm))


@pytest.mark.asyncio
async def test_a_call_without_an_outcome_is_reported_unknown(tmp_path, runner):
    _, service, harness, agent = _setup(tmp_path)

    async def lost(self, ctx, exception):
        return None  # As if the process died while the tool ran.

    with scripted(_model(_call("create_ticket", "c1", title="a"), create_text_response("done"))), patch.object(
        _Harness, "after_tool", lost,
    ):
        await run(agent, "file a ticket", "s1")
    plan = await _plan(harness)
    result = await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=plan.target_id))

    assert [e.action for e in plan.effects] == [EffectAction.UNKNOWN]
    assert service.open == {"T-1": "a"} and result.status == RollbackStatus.COMPLETED


@pytest.mark.asyncio
async def test_an_effect_without_the_undo_arguments_cannot_be_undone(tmp_path, runner):
    _, service, harness, agent = _setup(tmp_path)
    service.omit_id = True
    with scripted(_model(_call("create_ticket", "c1", title="a"), create_text_response("done"))):
        await run(agent, "file a ticket", "s1")
    plan = await _plan(harness)

    assert [e.action for e in plan.effects] == [EffectAction.CANNOT_UNDO]


@pytest.mark.asyncio
async def test_memory_is_kept_across_a_rollback(tmp_path, runner):
    ws, _, harness, agent = _setup(tmp_path)
    with scripted(_model(
        _call("put", "c1", path="memory/lesson.md", content="remember this"),
        _call("put", "c2", path="notes.txt", content="draft"),
        create_text_response("done"),
    )):
        await run(agent, "write", "s1")
        plan = await _plan(harness)
        await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=plan.target_id))

    assert (ws / "memory/lesson.md").read_text() == "remember this"
    assert not (ws / "notes.txt").exists()
