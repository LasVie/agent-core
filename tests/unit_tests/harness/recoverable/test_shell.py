# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-call declarations of shell commands, with the real bash tool on a local sys_operation.

Commands write under ``outside/``, a directory that is not in the workspace, so
only the declared reverse command can undo them.
"""
from __future__ import annotations

import json
import os
import signal
import uuid

import pytest
import pytest_asyncio

from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperationCard
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.rails import SysOperationRail
from openjiuwen.harness.recoverable import (
    EffectAction,
    EffectOutcome,
    RecoverableHarness,
    RollbackApproval,
    RollbackStatus,
)
from openjiuwen.harness.recoverable.processes import process_start_time
from tests.unit_tests.fixtures.deep_agent_interrupts import fake_model, run, scripted
from tests.unit_tests.fixtures.mock_llm import MockLLMModel, create_text_response, create_tool_call_response

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX shell and process groups")


@pytest_asyncio.fixture
async def runner():
    await Runner.start()
    try:
        yield
    finally:
        await Runner.stop()


def _agent(tmp_path, harness: RecoverableHarness | None):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    card = SysOperationCard(
        id=f"recoverable_shell_{uuid.uuid4().hex[:8]}", mode=OperationMode.LOCAL,
        # No allowlist: the test commands are local and harmless.
        work_config=LocalWorkConfig(work_dir=str(ws), shell_allowlist=None),
    )
    Runner.resource_mgr.add_sys_operation(card)
    rails = [SysOperationRail()] + ([harness.rail()] if harness is not None else [])
    return create_deep_agent(
        model=fake_model(), card=AgentCard(id=f"shell_{uuid.uuid4().hex[:8]}", name="RecoverableShell"),
        workspace=str(ws), sys_operation=Runner.resource_mgr.get_sys_operation(card.id), rails=rails,
        enable_task_loop=False, max_iterations=10, system_prompt="Follow the scripted model.",
    )


def _setup(tmp_path):
    harness = RecoverableHarness(kv_store=InMemoryKVStore(), storage_dir=tmp_path / "recoverable")
    outside = tmp_path / "outside"
    outside.mkdir()
    return harness, _agent(tmp_path, harness), outside


def _bash(call_id: str, command: str, **declaration):
    return create_tool_call_response("bash", json.dumps({"command": command, **declaration}), tool_call_id=call_id)


def _model(*responses) -> MockLLMModel:
    llm = MockLLMModel()
    llm.set_responses(list(responses) + [create_text_response("done")])
    return llm


async def _plan(harness, session_id: str = "s1"):
    target = next(c for c in await harness.history(session_id) if c.seq == 0)
    return await harness.plan_rollback(session_id, target.checkpoint_id)


async def _apply(harness, plan, **choices):
    return await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=plan.target_id, **choices))


@pytest.mark.asyncio
async def test_the_model_sees_the_declaration_arguments_only_with_the_harness(tmp_path, runner):
    with_harness = _agent(tmp_path / "a", RecoverableHarness(kv_store=InMemoryKVStore(),
                                                             storage_dir=tmp_path / "recoverable"))
    without = _agent(tmp_path / "b", None)
    for agent in (with_harness, without):
        await agent.ensure_initialized()

    infos = {t.name: t for t in await with_harness.react_agent.ability_manager.list_tool_info()}
    params = infos["bash"].parameters
    assert {"side_effect", "reverse_command", "external_paths"} <= set(params["properties"])
    assert "side_effect" in params["required"]
    plain = {t.name: t for t in await without.react_agent.ability_manager.list_tool_info()}
    assert "side_effect" not in plain["bash"].parameters["properties"]


@pytest.mark.asyncio
async def test_a_reversible_command_is_undone_by_its_reverse_command(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path)
    marker = outside / "marker"
    with scripted(_model(_bash("c1", f"echo made > {marker}", side_effect="reversible",
                               reverse_command=f"rm {marker}"))):
        await run(agent, "make it", "s1")
    assert marker.exists()

    plan = await _plan(harness)
    assert [(e.action, e.undo_args) for e in plan.effects] == [(EffectAction.UNDO, {"command": f"rm {marker}"})]
    result = await _apply(harness, plan)

    assert result.status == RollbackStatus.COMPLETED
    assert not marker.exists()


@pytest.mark.asyncio
async def test_the_reverse_of_a_failed_command_runs_only_when_confirmed(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path)
    first, second = outside / "first", outside / "second"
    with scripted(_model(
        _bash("c1", f"touch {first} && false", side_effect="reversible", reverse_command=f"rm -f {first}"),
        _bash("c2", f"touch {second} && false", side_effect="reversible", reverse_command=f"rm -f {second}"),
    )):
        await run(agent, "make them", "s1")
    plan = await _plan(harness)
    assert [e.action for e in plan.effects] == [EffectAction.REVIEW, EffectAction.REVIEW]

    newest = plan.effects[0]  # c2, the command that made `second`
    result = await _apply(harness, plan, confirmed_effect_ids=[newest.record_id])

    assert not second.exists(), "the confirmed reverse command ran"
    assert first.exists(), "an unconfirmed reverse command of a failed call does not run"
    assert [e.outcome for e in result.effects] == [EffectOutcome.UNDONE, None]


@pytest.mark.asyncio
async def test_outward_or_undeclared_commands_count_as_irreversible(tmp_path, runner):
    harness, agent, _ = _setup(tmp_path)
    with scripted(_model(
        _bash("c1", "ssh -V", side_effect="none"),
        _bash("c2", "echo hello"),
        _bash("c3", "echo quiet", side_effect="none"),
    )):
        await run(agent, "run", "s1")
    plan = await _plan(harness)

    assert [(e.action, e.detail) for e in plan.effects] == [
        (EffectAction.IRREVERSIBLE, "no side_effect was declared"),
        (EffectAction.IRREVERSIBLE, "declared without effect, but `ssh` acts outside the workspace"),
    ], "a command declared without effect and not outward is not recorded at all"


@pytest.mark.asyncio
async def test_a_reverse_command_is_checked_again_before_it_runs(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path)
    marker = outside / "marker"
    with scripted(_model(_bash("c1", f"touch {marker}", side_effect="reversible",
                               reverse_command=f"rm $(echo {marker})"))):
        await run(agent, "make it", "s1")
    result = await _apply(harness, await _plan(harness))

    assert result.status == RollbackStatus.PARTIAL
    assert result.effects[0].outcome == EffectOutcome.FAILED and "blocked" in result.effects[0].detail
    assert marker.exists()


@pytest.mark.asyncio
async def test_a_background_process_started_after_the_target_is_stopped(tmp_path, runner):
    harness, agent, _ = _setup(tmp_path)
    with scripted(_model(_bash("c1", "sleep 60", side_effect="none", run_in_background=True))):
        await run(agent, "start a server", "s1")
    plan = await _plan(harness)
    [pid] = plan.processes_to_stop
    try:
        assert process_start_time(pid) is not None
        result = await _apply(harness, plan)
        assert result.processes_stopped == [pid]
        assert process_start_time(pid) is None
        assert plan.effects == [], "a background launch without effect is not an effect"
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
