#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""External (non-Playwright) tools taking part in the Browser runtime."""
# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.browser_move.playwright_runtime import browser_remaining_time, external_tool_role
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime import (
    BrowserAgentRuntime,
    BrowserRuntimeRail,
)

STATE_KEY = "__browser_phase_budget_state__"


def _run(coro):
    return asyncio.run(coro)


class _FakeSession:
    def __init__(self, task: str = "Find the title") -> None:
        self._state = {STATE_KEY: BrowserRuntimeRail._build_phase_state(task)}

    def get_session_id(self) -> str:
        return "browser-session"

    def get_state(self, key: str):
        return self._state.get(key)

    def update_state(self, payload):
        self._state.update(payload)

    @property
    def state(self) -> dict:
        return self._state[STATE_KEY]


class _ExternalTool(Tool):
    """A site-command-like tool declared through card properties only."""

    accepts_tool_callback_context = True

    def __init__(self, name: str, role: str | None) -> None:
        properties = {"browser_runtime": {"role": role, "windowed": True}} if role else {}
        super().__init__(
            ToolCard(
                name=name,
                description="external",
                input_params={"type": "object", "properties": {"q": {"type": "string"}}},
                properties=properties,
            )
        )
        self.remaining: Any = "unset"

    async def invoke(self, inputs, **kwargs):
        self.remaining = browser_remaining_time(kwargs.get("_tool_callback_context"))
        return ToolOutput(success=True, data={"ok": True})

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


@pytest.fixture
def manager():
    tools = [_ExternalTool("ext_lookup", "discovery"), _ExternalTool("ext_run", "action"),
             _ExternalTool("ext_plain", None)]
    Runner.resource_mgr.add_tool(tools)
    ability_manager = AbilityManager()
    for tool in tools:
        ability_manager.add(tool.card)
    yield ability_manager, {tool.card.name: tool for tool in tools}
    for tool in tools:
        Runner.resource_mgr.remove_tool(tool_id=tool.card.id)


def _rail() -> tuple[BrowserRuntimeRail, MagicMock]:
    runtime = MagicMock(spec=BrowserAgentRuntime)
    return BrowserRuntimeRail(runtime), runtime


def _ctx(ability_manager: AbilityManager, session: _FakeSession, inputs: ToolCallInputs) -> AgentCallbackContext:
    agent = SimpleNamespace(ability_manager=ability_manager)
    return AgentCallbackContext(agent=agent, inputs=inputs, session=session)


def test_remaining_time_uses_the_earlier_deadline() -> None:
    now = time.monotonic()
    ctx = SimpleNamespace(extra={"__browser_task_deadline__": now + 100, "__browser_invocation_deadline__": now + 40})
    assert 35 < browser_remaining_time(ctx) <= 40
    assert browser_remaining_time(SimpleNamespace(extra={})) is None
    assert browser_remaining_time(None) is None
    expired = SimpleNamespace(extra={"__browser_task_deadline__": now - 5})
    assert browser_remaining_time(expired) == 0.0


def test_role_is_read_from_card_properties() -> None:
    assert external_tool_role(ToolCard(name="a", properties={"browser_runtime": {"role": "action"}})) == "action"
    assert external_tool_role(ToolCard(name="b", properties={"browser_runtime": {"role": "other"}})) is None
    assert external_tool_role(ToolCard(name="c")) is None


def test_callback_context_reaches_only_opted_in_tools_through_ability_manager(manager) -> None:
    ability_manager, tools = manager
    callback_manager = SimpleNamespace(execute=AsyncMock())
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(agent_callback_manager=callback_manager),
        extra={"__browser_task_deadline__": time.monotonic() + 100},
    )
    _run(ability_manager.execute(ctx=ctx, tool_call=ToolCall(id="1", type="function", name="ext_run",
                                                               arguments="{}"), session=None))
    assert 95 < tools["ext_run"].remaining <= 100
    tools["ext_plain"].accepts_tool_callback_context = False
    _run(ability_manager.execute(ctx=ctx, tool_call=ToolCall(id="2", type="function", name="ext_plain",
                                                               arguments="{}"), session=None))
    assert tools["ext_plain"].remaining is None


def test_rail_resolves_roles_through_the_real_ability_manager(manager) -> None:
    ability_manager, _ = manager
    ctx = _ctx(ability_manager, _FakeSession(), ToolCallInputs())
    assert BrowserRuntimeRail._external_tool_role(ctx, "ext_lookup") == "discovery"
    assert BrowserRuntimeRail._external_tool_role(ctx, "ext_run") == "action"
    assert BrowserRuntimeRail._external_tool_role(ctx, "ext_plain") is None
    assert BrowserRuntimeRail._external_tool_role(ctx, "unknown_tool") is None
    assert BrowserRuntimeRail._external_tool_role(ctx, "browser_snapshot") is None


def test_discovery_is_exempt_and_action_has_its_own_budget(manager) -> None:
    ability_manager, _ = manager
    rail, _ = _rail()
    session = _FakeSession()
    navigation_before = dict(session.state["phases"]["navigation"])
    for index in range(20):
        ctx = _ctx(ability_manager, session, ToolCallInputs(tool_name="ext_lookup", tool_args={"q": str(index)}))
        _run(rail.before_tool_call(ctx))
    assert session.state["phases"]["navigation"] == navigation_before
    assert "external_actions" not in session.state

    ctx = _ctx(ability_manager, session, ToolCallInputs(tool_name="ext_run", tool_args={"q": "a"}))
    _run(rail.before_tool_call(ctx))
    assert session.state["external_actions"]["attempts"] == 1
    assert session.state["phases"]["navigation"]["attempts"] == navigation_before["attempts"]


def test_identical_external_command_is_limited_and_budget_is_bounded() -> None:
    session = _FakeSession()
    for _ in range(2):
        BrowserRuntimeRail._consume_phase_budget(session, "ext_run", {"q": "same"}, external_role="action")
    with pytest.raises(ValueError, match="already ran 2 times"):
        BrowserRuntimeRail._consume_phase_budget(session, "ext_run", {"q": "same"}, external_role="action")
    session.state["external_actions"]["attempts"] = session.state["external_actions"]["budget"]
    with pytest.raises(ValueError, match="budget exhausted"):
        BrowserRuntimeRail._consume_phase_budget(session, "ext_run", {"q": "new"}, external_role="action")


def test_external_commands_use_complete_arguments_and_ignore_mapping_order() -> None:
    session = _FakeSession()
    shared = {"site": "alpha", "command": "list", "args": {"query": "x" * 1000 + "a", "limit": 3}}
    different_calls = [
        shared,
        {**shared, "site": "beta"},
        {**shared, "command": "search"},
        {**shared, "args": {"query": "x" * 1000 + "b", "limit": 3}},
    ]
    for args in different_calls:
        for _ in range(2):
            BrowserRuntimeRail._consume_phase_budget(session, "ext_run", args, external_role="action")
    assert session.state["external_actions"]["attempts"] == 8
    assert len(session.state["external_actions"]["signatures"]) == 4
    reordered = {"args": {"limit": 3, "query": shared["args"]["query"]}, "command": "list", "site": "alpha"}
    with pytest.raises(ValueError, match="already ran 2 times"):
        BrowserRuntimeRail._consume_phase_budget(session, "ext_run", json.dumps(reordered), external_role="action")
    assert session.state["external_actions"]["attempts"] == 8


def test_distinct_generic_external_calls_still_exhaust_the_task_budget() -> None:
    session = _FakeSession()
    for index in range(16):
        BrowserRuntimeRail._consume_phase_budget(session, "ext_run", {"q": str(index)}, external_role="action")
    assert session.state["external_actions"]["attempts"] == 16
    with pytest.raises(ValueError, match="budget exhausted"):
        BrowserRuntimeRail._consume_phase_budget(session, "ext_run", {"q": "new"}, external_role="action")
    assert session.state["external_actions"]["attempts"] == 16


def _after(rail, ability_manager, session, name, data, success=True):
    message = ToolMessage(content="{}", tool_call_id="call-1")
    inputs = ToolCallInputs(
        tool_name=name,
        tool_args={"q": "gpu"},
        tool_result=ToolOutput(success=success, data=data, error=None if success else "failed"),
        tool_msg=message,
    )
    ctx = _ctx(ability_manager, session, inputs)
    _run(rail.after_tool_call(ctx))
    return ctx, message


def test_external_results_become_evidence_without_touching_the_page(manager) -> None:
    ability_manager, _ = manager
    rail, runtime = _rail()
    session = _FakeSession("列出前 3 条视频的标题")
    assert session.state["requested_result_count"] == 3
    data = {
        "ok": True, "reason": "ok", "observed_count": 3,
        "data": [{"title": "a"}, {"title": "b"}, {"title": "c"}],
        "extracted": {"title": "a"},
        "field_provenance": {"title": {"source": "opencli:demo/search"}},
        "source": {"provider": "opencli", "command": "demo/search"},
    }
    _, message = _after(rail, ability_manager, session, "ext_run", data)

    assert message.metadata["state_changed"] is False
    runtime.record_tool_reference_state.assert_not_called()
    kinds = {item["kind"] for item in session.state["structured_evidence"]}
    assert {"structured_extraction", "external_observation"} <= kinds
    assert session.state["observed_result_count"] == 3
    assert not any(item.startswith("result_count") for item in BrowserRuntimeRail._missing_completion_requirements(session.state))
    assert BrowserRuntimeRail._task_observations(session.state)


def test_failed_external_result_records_no_evidence(manager) -> None:
    ability_manager, _ = manager
    rail, _ = _rail()
    session = _FakeSession()
    _, message = _after(rail, ability_manager, session, "ext_run", {"ok": False, "reason": "error"}, success=False)
    assert message.metadata["success"] is False and message.metadata["state_changed"] is False
    assert not session.state.get("structured_evidence")


def test_user_action_ends_the_task_as_blocked(manager) -> None:
    ability_manager, _ = manager
    rail, _ = _rail()
    session = _FakeSession()
    data = {
        "ok": False, "reason": "needs_user",
        "user_action_required": {"reason": "captcha", "instructions": "solve it in the sidebar"},
    }
    ctx, _ = _after(rail, ability_manager, session, "ext_run", data, success=False)
    assert session.state["status"] == "blocked"
    assert session.state["blockers"][0] == "captcha"
    assert session.state["terminal_reason"] == "external_user_action_required"
    request = ctx.consume_force_finish()
    assert request is not None
    payload = BrowserRuntimeRail._authoritative_terminal_payload(session.state)
    assert payload["retryable"] is False
    assert any("solve it in the sidebar" in blocker for blocker in payload["blockers"])


def test_unknown_user_action_reason_falls_back_to_intervention(manager) -> None:
    ability_manager, _ = manager
    rail, _ = _rail()
    session = _FakeSession()
    _after(rail, ability_manager, session, "ext_run",
           {"ok": False, "user_action_required": {"reason": "please-retry"}}, success=False)
    assert session.state["blockers"] == ["user_intervention_required"]


def _hot_rows(*ids: str, author: bool = True) -> list[dict]:
    return [
        {"title": f"video {item}", "url": f"https://www.bilibili.com/video/{item}",
         **({"author": f"up {item}"} if author else {})}
        for item in ids
    ]


def test_uploader_is_a_requested_author_field() -> None:
    assert "author" in BrowserRuntimeRail._infer_required_fields("返回B站热门前 3 条视频的标题、UP主和链接")
    assert "author" in BrowserRuntimeRail._infer_required_fields("List the top 3 videos with title, uploader and link")


def test_top_n_needs_n_rows_with_every_requested_field(manager) -> None:
    ability_manager, _ = manager
    rail, _ = _rail()
    session = _FakeSession("返回B站热门前 3 条视频的标题、UP主和链接")
    rows = _hot_rows("BV1", "BV2", "BV3")
    rows[1].pop("author")
    rows[2].pop("author")
    data = {
        "ok": True, "reason": "ok", "observed_count": 3, "data": rows, "records": rows,
        "extracted": rows[0], "source": {"provider": "opencli", "command": "bilibili/hot"},
    }
    _after(rail, ability_manager, session, "ext_run", data)

    # Three rows were observed, but only one carries every requested field.
    assert "result_count:1/3" in BrowserRuntimeRail._missing_completion_requirements(session.state)

    complete = _hot_rows("BV1", "BV2", "BV3")
    _after(rail, ability_manager, session, "ext_run", {**data, "data": complete, "records": complete})
    assert not any(
        item.startswith("result_count") for item in BrowserRuntimeRail._missing_completion_requirements(session.state)
    )
    payload = BrowserRuntimeRail._authoritative_terminal_payload(session.state)
    assert [row["url"] for row in payload["records"]] == [row["url"] for row in complete]
    assert payload["records_source"] == "external:bilibili/hot" and payload["backed_result_count"] == 3


def test_count_from_one_source_does_not_back_rows_from_another(manager) -> None:
    ability_manager, _ = manager
    rail, _ = _rail()
    session = _FakeSession("返回B站热门前 3 条视频的标题和链接")
    rows = _hot_rows("BV1")
    data = {
        "ok": True, "reason": "ok", "observed_count": 3, "data": rows, "records": rows,
        "extracted": rows[0], "source": {"provider": "opencli", "command": "bilibili/hot"},
    }
    _after(rail, ability_manager, session, "ext_run", data)
    # A bare count of 3 no longer satisfies "top 3" once rows are reported.
    assert "result_count:1/3" in BrowserRuntimeRail._missing_completion_requirements(session.state)


def test_summary_links_without_evidence_are_flagged_but_status_is_kept(manager) -> None:
    ability_manager, _ = manager
    rail, _ = _rail()
    session = _FakeSession("返回B站热门前 2 条视频的标题和链接")
    rows = _hot_rows("BV1", "BV2")
    data = {
        "ok": True, "reason": "ok", "observed_count": 2, "data": rows, "records": rows,
        "extracted": rows[0], "source": {"provider": "opencli", "command": "bilibili/hot"},
    }
    _after(rail, ability_manager, session, "ext_run", data)
    session.state["status"] = "completed"
    summary = (
        "1. video BV1 https://www.bilibili.com/video/BV1/\n"
        "2. other https://www.bilibili.com/video/BVX."
    )
    _, payload = BrowserRuntimeRail._render_authoritative_terminal_output(session.state, summary)
    assert payload["status"] == "completed"
    assert payload["unbacked_items"] == ["https://www.bilibili.com/video/BVX"]

    _, payload = BrowserRuntimeRail._render_authoritative_terminal_output(
        session.state, "1. https://www.bilibili.com/video/BV1 2. https://www.bilibili.com/video/BV2"
    )
    assert "unbacked_items" not in payload
