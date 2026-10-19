# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A selection that leaves the final deliverables out is a look at the process, not at the result."""

from __future__ import annotations

import json
from typing import Any

import pytest

from openjiuwen.agent_evolving.attribution.model import DiagnosticSignal, SignalSource, TaskParseResult
from openjiuwen.agent_evolving.attribution.nodes import TrajectoryNode
from openjiuwen.agent_evolving.attribution.reward.fast_reward_adapter import final_reply, review_scope
from openjiuwen.agent_evolving.attribution.run import NodeBackedRuntime, run_attribution, run_diagnosis
from openjiuwen.agent_evolving.attribution.snapshot import slice_nodes_by_range
from openjiuwen.agent_evolving.attribution.task_parse import last_writes, run_task_parse
from openjiuwen.agent_evolving.attribution.tools import build_attribution_tools

pytestmark = pytest.mark.asyncio

LEADER = "team-member:s:t:team-leader"
MEMBER = "team-member:s:t:writer"


def _node(
    key: str,
    seq: int,
    ts: int,
    role: str = "assistant",
    content: str = "",
    calls: list[dict[str, Any]] | None = None,
    kind: str = "team_leader",
) -> TrajectoryNode:
    return TrajectoryNode(
        node_id=f"{key}:{seq}",
        subagent_key=key,
        display_name=key,
        kind=kind,
        role=role,
        content=content or None,
        tool_calls=calls or [],
        start_ts_ns=ts,
        end_ts_ns=ts + 1,
    )


def _write(path: str) -> list[dict[str, Any]]:
    return [{"name": "write_file", "arguments": json.dumps({"file_path": path, "content": "x"})}]


def _send(path: str) -> list[dict[str, Any]]:
    return [{"name": "send_file_to_user", "arguments": json.dumps({"abs_file_path_list": [path]})}]


def _session() -> dict[str, list[TrajectoryNode]]:
    request = json.dumps({"type": "user input", "content": "排周六，写 schedule.md 和给外婆的消息"}, ensure_ascii=False)
    return {
        LEADER: [
            _node(LEADER, 1, 10, "user", f"你收到一条消息：\n{request}"),
            _node(LEADER, 2, 20, content="团队已组建"),
            _node(LEADER, 3, 40, calls=_write("/w/schedule.md")),
            _node(LEADER, 4, 48, content="初稿已写好"),
            _node(LEADER, 5, 80, calls=_write("/w/schedule.md")),  # rewritten after the selection
            _node(LEADER, 6, 85, calls=_send("/w/message.md")),  # sending is not writing
            _node(LEADER, 7, 90, content="都做好了：schedule.md 与 message.md"),
        ],
        MEMBER: [
            _node(MEMBER, 1, 30, "user", "请写给外婆的消息", kind="team_member"),
            _node(MEMBER, 2, 45, calls=_write("/w/message.md"), kind="team_member"),
            _node(MEMBER, 3, 46, content="消息已写好，交给 leader", kind="team_member"),
        ],
    }


class _Llm:
    async def invoke(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        del messages, tools, kwargs
        return {
            "tool_calls": [
                {"name": "report_task_description", "arguments": json.dumps({"task": "排周六"})},
                {
                    "name": "report_message_artifacts",
                    "arguments": json.dumps({"node_ids": [f"{MEMBER}:3", f"{LEADER}:4"]}),
                },
                {
                    "name": "report_file_artifacts",
                    "arguments": json.dumps({"paths": ["/w/schedule.md", "/w/message.md"]}),
                },
            ]
        }


def test_last_writes_skip_sending_a_file() -> None:
    assert last_writes(_session()) == {"/w/schedule.md": f"{LEADER}:5", "/w/message.md": f"{MEMBER}:2"}


def test_last_writes_follow_time_across_subjects() -> None:
    nodes = _session()
    nodes[MEMBER].append(_node(MEMBER, 4, 95, calls=_write("/w/schedule.md"), kind="team_member"))
    assert last_writes(nodes)["/w/schedule.md"] == f"{MEMBER}:4"  # the member rewrote it after the leader


async def test_task_parse_keeps_only_deliverables_whose_final_version_is_selected() -> None:
    sliced = slice_nodes_by_range(_session(), [(LEADER, 10, 50), (MEMBER, 30, 50)])
    result = await run_task_parse(sliced, "zh", _Llm())
    assert result.file_artifacts == ["/w/message.md"]  # its last write (by the member) is selected
    assert result.deliverables_outside_selection == ["/w/schedule.md"]  # rewritten at 80, outside
    assert result.message_artifacts == [(f"{LEADER}:4", "初稿已写好")]  # a member's message is team-internal
    assert result.selection_partial is True
    assert result.run_meta["team_internal_messages_dropped"] == 1
    assert result.run_meta["files_outside_selection"] == 1


async def test_task_parse_on_the_whole_session_keeps_everything() -> None:
    whole = _session()
    sliced = slice_nodes_by_range(whole, [(LEADER, 0, 100), (MEMBER, 0, 100)])
    result = await run_task_parse(sliced, "zh", _Llm())
    assert result.file_artifacts == ["/w/schedule.md", "/w/message.md"]
    assert result.deliverables_outside_selection == [] and result.selection_partial is False


def test_review_scope_names_what_is_reviewed() -> None:
    parse = TaskParseResult(
        task_description="排周六",
        message_artifacts=[],
        file_artifacts=["/w/message.md"],
        file_candidates=[],
        deliverables_outside_selection=["/w/schedule.md"],
        selection_partial=True,
    )
    assert final_reply(parse, _session()) == ""  # no fallback to a progress message in a partial selection
    scope = review_scope(parse, ["/w/message.md"], "", "zh")
    assert "message.md" in scope.splitlines()[0]
    assert "不评估回复" in scope and "schedule.md" in scope.splitlines()[-1]
    whole = TaskParseResult(task_description="排周六", message_artifacts=[], file_artifacts=[], file_candidates=[])
    assert review_scope(whole, ["/w/a.md"], "回复", "zh") == ""  # the whole session needs no scope


async def test_no_signal_is_no_problem() -> None:
    parse = TaskParseResult(task_description="排周六", message_artifacts=[], file_artifacts=[], file_candidates=[])
    nodes = {LEADER: [_node(LEADER, 2, 20, content="团队已组建")]}
    result = await run_diagnosis(
        run_id="r", language="zh", nodes_by_subagent=nodes, user_attribution="", task_parse=parse
    )
    assert [signal.tier for signal in result.signals] == ["perfect"]
    assert result.signals[0].text.startswith("未发现过程问题")
    outcome = await run_attribution(
        model=object(), nodes_by_subagent=nodes, signals=result.signals, task_description="排周六"
    )
    assert outcome.status == "success" and outcome.skipped_signal_ids == [result.signals[0].signal_id]


async def _invoke(tool: Any, **inputs: Any) -> dict[str, Any]:
    output = await tool.invoke(inputs)
    assert output.success
    return json.loads(output.data["content"])


async def test_no_artifact_improvement_when_no_deliverable_was_reviewed() -> None:
    nodes = {LEADER: [_node(LEADER, 2, 20, content="团队已组建")]}
    signal = DiagnosticSignal.create("write_file: Error", source=SignalSource.KEYWORD_TOOL, node_ids=[f"{LEADER}:2"])
    runtime = NodeBackedRuntime(nodes)
    runtime.artifact_improvement_required = False  # what run_attribution sets without reward signals
    tools = {tool.tool_name: tool for tool in build_attribution_tools(runtime, [signal.signal_id])}
    assert "error" in await _invoke(tools["submit_artifact_improvement"], md_string="改产物")
    assert runtime.artifact_improvement is None
    await _invoke(tools["mark_signal_invalid"], diag_signal_id=signal.signal_id)
    assert (await _invoke(tools["check_attribution_complete"]))["all_done"] is True
