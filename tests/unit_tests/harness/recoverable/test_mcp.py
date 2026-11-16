# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP tools on a real DeepAgent, served by a local stdio MCP server.

An MCP tool missing from the declaration table follows its server's
``readOnlyHint``; a host entry overrides the hint.
"""
from __future__ import annotations

import json
import uuid

import pytest

from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.recoverable import EffectAction, EffectCategory, EffectDeclaration, RecoverableHarness
from tests.unit_tests.core.foundation.tool.mcp.test_tool_annotations import notes_server_config
from tests.unit_tests.fixtures.deep_agent_interrupts import fake_model, run, scripted
from tests.unit_tests.fixtures.mock_llm import create_text_response, create_tool_call_response
from tests.unit_tests.harness.recoverable.test_harness import _model, runner  # noqa: F401


async def _run_notes(tmp_path, declarations=None) -> tuple[RecoverableHarness, list]:
    harness = RecoverableHarness(kv_store=InMemoryKVStore(), storage_dir=tmp_path / "recoverable",
                                 declarations=declarations)
    ws = tmp_path / "ws"
    ws.mkdir()
    agent = create_deep_agent(
        model=fake_model(), card=AgentCard(id=f"mcp_{uuid.uuid4().hex[:8]}", name="RecoverableMcp"),
        workspace=str(ws), mcps=[notes_server_config(tmp_path)], rails=[harness.rail()],
        enable_task_loop=False, max_iterations=10, system_prompt="Follow the scripted model.",
    )
    with scripted(_model(
        create_tool_call_response("mcp_notes_lookup", json.dumps({"key": "a"}), tool_call_id="c1"),
        create_tool_call_response("mcp_notes_write", json.dumps({"key": "a", "value": "1"}), tool_call_id="c2"),
        create_text_response("done"),
    )):
        await run(agent, "take notes", "s1")
    assert (tmp_path / "writes.log").read_text() == "a=1\n", "the MCP tool ran on the server"
    target = (await harness.history("s1"))[-1]
    plan = await harness.plan_rollback("s1", target.checkpoint_id)
    return harness, plan.effects


@pytest.mark.asyncio
async def test_mcp_tools_follow_their_servers_read_only_hint(tmp_path, runner):
    _, effects = await _run_notes(tmp_path)

    assert [(e.action, e.tool_name, e.detail) for e in effects] == [
        (EffectAction.IRREVERSIBLE, "mcp_notes_write", "an MCP tool its server does not mark read-only"),
    ], "the read-only lookup is not recorded at all"


@pytest.mark.asyncio
async def test_a_host_declaration_overrides_the_servers_hint(tmp_path, runner):
    declarations = {"mcp_notes_write": EffectDeclaration(category=EffectCategory.NONE)}
    _, effects = await _run_notes(tmp_path, declarations)

    assert effects == []
