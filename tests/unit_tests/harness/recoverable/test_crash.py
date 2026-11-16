# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A rollback cut short by a crash, finished after a restart (V-16).

The crash is an exception no handler catches, raised at a point of the rollback.
The restart is a new harness and agent over the same storage, with the old
process's hold on the workspace released, as its exit would.
"""
from __future__ import annotations

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.harness.recoverable import (
    EffectOutcome,
    RecoverableHarness,
    RollbackApproval,
    RollbackSignal,
    RollbackStatus,
    SignalStatus,
)
from openjiuwen.harness.recoverable.store import CheckpointStore
from tests.unit_tests.fixtures.deep_agent_interrupts import make_deep_agent, messages_seen, run, scripted
from tests.unit_tests.fixtures.mock_llm import create_text_response
from tests.unit_tests.harness.recoverable.test_ledger import (  # noqa: F401
    DECLARATIONS,
    CloseTicket,
    CreateTicket,
    SendMessage,
    TicketService,
    WriteFile,
    _call,
    _model,
    runner,
)


class Crash(BaseException):
    """The process dies here; nothing catches it."""


class World:
    """One process: a harness and an agent over shared storage, a ticket service and a workspace."""

    def __init__(self, tmp_path, kv, service):
        self.ws = tmp_path / "ws"
        self.ws.mkdir(exist_ok=True)
        self.harness = RecoverableHarness(kv_store=kv, storage_dir=tmp_path / "recoverable",
                                          declarations=DECLARATIONS)
        tools = [CreateTicket(service), CloseTicket(service), SendMessage(service), WriteFile(self.ws)]
        self.agent = make_deep_agent(self.ws, tools=tools, rails=[self.harness.rail()], name="RecoverableCrash")


def _crash_after(monkeypatch, owner, name: str, *, calls: int = 1, before: bool = False):
    """Make ``owner.name`` crash on its ``calls``-th call, before or after doing its work."""
    real = getattr(owner, name)
    count = 0

    async def crashing(*args, **kwargs):
        nonlocal count
        count += 1
        if count == calls and before:
            raise Crash(name)
        result = await real(*args, **kwargs)
        if count == calls:
            raise Crash(name)
        return result

    monkeypatch.setattr(owner, name, crashing)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", [
    "after stopping processes", "between two undos", "after the undos", "at the commit", "in the commit",
])
async def test_an_interrupted_rollback_is_finished_after_a_restart(tmp_path, runner, monkeypatch, point):
    kv, service = InMemoryKVStore(), TicketService()
    first = World(tmp_path, kv, service)
    llm = _model(
        _call("create_ticket", "c1", title="one"), _call("put", "c2", path="a.txt", content="v1"),
        _call("create_ticket", "c3", title="two"), create_text_response("done"),
        create_text_response("after the rollback"),
    )
    with scripted(llm):
        await run(first.agent, "work", "s1")
        target = (await first.harness.history("s1"))[-1]
        plan = await first.harness.plan_rollback("s1", target.checkpoint_id)
        with monkeypatch.context() as patch:
            harness = first.harness
            if point == "after stopping processes":
                _crash_after(patch, harness, "_stop_processes")
            elif point == "between two undos":
                _crash_after(patch, harness, "_undo", calls=2, before=True)
            elif point == "after the undos":
                _crash_after(patch, harness, "_restore_dependencies", before=True)
            elif point == "at the commit":
                _crash_after(patch, harness, "_abandon_records", before=True)
            else:  # The note is written, HEAD is not moved yet.
                _crash_after(patch, CheckpointStore, "set_head", before=True)
            with pytest.raises(Crash):
                await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=target.checkpoint_id))
        first.harness.release_session("s1")

        second = World(tmp_path, kv, service)
        await second.agent.ensure_initialized()
        with pytest.raises(BaseError, match="was interrupted"):
            await run(second.agent, "go on", "s1")
        signal = RollbackSignal(signal_id="late", session_id="s1", target_id=target.checkpoint_id)
        assert (await second.harness.request_rollback(signal)).status == SignalStatus.REJECTED

        result = await second.harness.resume_rollback(plan.plan_id)

        assert result.status == RollbackStatus.COMPLETED
        assert service.closed == ["T-2", "T-1"], "each ticket is closed once, newest first"
        assert not (second.ws / "a.txt").exists()
        assert [c.checkpoint_id for c in await second.harness.history("s1")] == [target.checkpoint_id]
        await run(second.agent, "go on", "s1")

    contents = [str(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in messages_seen(llm)]
    notes = "".join(text for text in contents if text.startswith("[ROLLBACK]"))  # Pending notes share a message.
    assert notes.count("[ROLLBACK]") == 1, "the note is written once, even when the commit ran twice"
    assert "(0 written back, 1 deleted)" in notes, "the note reports what the stages before the crash did"


@pytest.mark.asyncio
async def test_an_undo_cut_short_is_reported_not_run_again(tmp_path, runner, monkeypatch):
    kv, service = InMemoryKVStore(), TicketService()
    first = World(tmp_path, kv, service)
    with scripted(_model(_call("create_ticket", "c1", title="one"), create_text_response("done"))):
        await run(first.agent, "work", "s1")
    target = (await first.harness.history("s1"))[-1]
    plan = await first.harness.plan_rollback("s1", target.checkpoint_id)
    with monkeypatch.context() as patch:
        _crash_after(patch, first.harness, "_invoke_tool")  # The ticket is closed; its record is not completed.
        with pytest.raises(Crash):
            await first.harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=target.checkpoint_id))
    first.harness.release_session("s1")

    second = World(tmp_path, kv, service)
    await second.agent.ensure_initialized()
    result = await second.harness.resume_rollback(plan.plan_id)

    assert result.status == RollbackStatus.PARTIAL
    assert result.effects[0].outcome == EffectOutcome.FAILED and "check it by hand" in result.effects[0].detail
    assert service.closed == ["T-1"], "an undo whose outcome is unknown is not run again"
