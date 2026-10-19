# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fast Reward as the Reward stage of the attribution pipeline."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.agent_evolving.attribution import run as run_module
from openjiuwen.agent_evolving.attribution.model import DiagnosticSignal, SignalSource, TaskParseResult
from openjiuwen.agent_evolving.attribution.nodes import TrajectoryNode
from openjiuwen.agent_evolving.attribution.reward import fast_reward_adapter as adapter
from openjiuwen.agent_evolving.attribution.run import run_diagnosis

LEADER = "team-member:s:t:team-leader"
MEMBER = "team-member:s:t:writer"


def _fast_result(total: Any = 72) -> dict[str, Any]:
    item = {"category_id": "plan", "category_name": "日程", "comment": ""}
    return {
        "meta": {"warnings": []},
        "total": {"score": total, "summary": "时间表可用，外婆到达时间与约束冲突"},
        "categories": [{"category_id": "plan", "category_name": "日程", "comment": "一处冲突", "score": 70}],
        "sub_category_items": [
            {**item, "rubric": "每段时间都排得开", "status": "met", "type": "floor"},
            {
                **item,
                "rubric": "外婆到达在 5 点以后",
                "status": "unmet",
                "type": "floor",
                "comment": "16:55 到，早于 5 点",
            },
            {**item, "rubric": "购物清单标出顺路时段", "status": "partial", "type": "floor", "comment": "牛奶缺时段"},
            {**item, "rubric": "预判师傅超时的应对", "status": "unmet", "type": "proactive_challenge"},
            {**item, "rubric": "泳镜货源可靠", "status": "unknown", "type": "floor"},
        ],
    }


def _node(key: str, seq: int, ts: int, kind: str) -> TrajectoryNode:
    return TrajectoryNode(
        node_id=f"{key}:{seq}",
        subagent_key=key,
        display_name=key,
        kind=kind,
        role="assistant",
        content="x",
        start_ts_ns=ts,
        end_ts_ns=ts + 1,
    )


def _nodes() -> dict[str, list[TrajectoryNode]]:
    return {
        LEADER: [_node(LEADER, 1, 10, "team_leader"), _node(LEADER, 2, 90, "team_leader")],
        MEMBER: [_node(MEMBER, 1, 95, "team_member")],
    }


def _task_parse(tmp_path: Any) -> TaskParseResult:
    deliverable = tmp_path / "schedule.md"
    deliverable.write_text("# 周六", encoding="utf-8")
    return TaskParseResult(
        task_description="安排周六",
        message_artifacts=[(f"{LEADER}:1", "团队已组建"), (f"{MEMBER}:1", "已写入"), (f"{LEADER}:2", "最终答复")],
        file_artifacts=[str(deliverable), str(tmp_path / "missing.md")],
        file_candidates=[],
        user_request="帮我安排周六：外婆 5 点以后有空",
    )


def test_claims_become_dimensions_with_tiers_and_unknowns_are_counted() -> None:
    payload = adapter.to_reward_payload(_fast_result(), "zh")
    assert payload["overall"] == {
        "comment": "时间表可用，外婆到达时间与约束冲突 另有 1 项因证据不足未能判定。",
        "total_score": 72,
    }
    assert [(d["requirement"], d["score"]) for d in payload["dimensions"]] == [
        ("[要求] 每段时间都排得开", 100),
        ("[要求] 外婆到达在 5 点以后", 40),
        ("[要求] 购物清单标出顺路时段", 80),
        ("[挑战] 预判师傅超时的应对", 80),
    ]
    assert payload["dimensions"][1]["comment"] == "16:55 到，早于 5 点"
    assert payload["dimensions"][0]["comment"] == "每段时间都排得开"  # no reason: the claim itself
    assert payload["categories"] == [{"name": "日程", "comment": "一处冲突", "score": 70}]
    with pytest.raises(ValueError):
        adapter.to_reward_payload(_fast_result(total=None))
    with pytest.raises(ValueError):
        adapter.to_reward_payload({"total": {"score": 50}, "sub_category_items": [{"status": "unknown"}]})


def test_final_reply_is_the_last_message_the_user_got(tmp_path: Any) -> None:
    assert adapter.final_reply(_task_parse(tmp_path), _nodes()) == "最终答复"


def test_route_from_model_reads_the_core_model_and_refuses_a_bare_one() -> None:
    model = SimpleNamespace(
        model_client_config=SimpleNamespace(
            api_base="https://example.com/v1", api_key="k", client_provider="OpenAI", timeout=300
        ),
        model_config=SimpleNamespace(model_name="qwen3.7-plus", extra_body={"enable_thinking": False}),
    )
    assert adapter.route_from_model(model) == {
        "model": "qwen3.7-plus",
        "base_url": "https://example.com/v1",
        "api_key": "k",
        "provider": "OpenAI",
        "timeout": 300.0,
        "extra_body": {"enable_thinking": False},
    }
    with pytest.raises(ValueError):
        adapter.route_from_model(object())


def _keyword(signals: list[DiagnosticSignal]) -> Any:
    async def fake(*args: Any, **kwargs: Any) -> tuple[list[DiagnosticSignal], dict[str, Any]]:
        del args, kwargs
        return signals, {"mode": "rule"}

    return fake


async def test_run_diagnosis_scores_with_fast_reward_and_keeps_process_signals(monkeypatch: Any, tmp_path: Any) -> None:
    seen: dict[str, Any] = {}

    async def fake_reward(task: str, artifacts: list[str], config: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seen.update(task=task, artifacts=artifacts, config=config, **kwargs)
        return _fast_result()

    from openjiuwen.agent_evolving.fast_reward import pipeline

    monkeypatch.delenv("ATTRIBUTION_REWARD", raising=False)
    monkeypatch.setattr(pipeline, "reward", fake_reward)
    monkeypatch.setattr(adapter, "fast_reward_config", lambda model: {"producer": {}, "judge": {}})
    monkeypatch.setattr(run_module, "build_reward_agent", lambda **kwargs: pytest.fail("the Reward agent ran"))
    complaint = DiagnosticSignal.create(
        "没有路径我怎么知道去哪里看文件", source=SignalSource.KEYWORD_USER, node_ids=[f"{LEADER}:1"]
    )
    monkeypatch.setattr(run_module, "run_keyword_diagnosis", _keyword([complaint]))
    events: list[Any] = []
    result = await run_diagnosis(
        run_id="r",
        language="zh",
        nodes_by_subagent=_nodes(),
        user_attribution="外婆的时间要核对",
        task_parse=_task_parse(tmp_path),
        model=object(),
        status_sink=SimpleNamespace(report=events.append),
    )
    assert seen["task"] == "帮我安排周六：外婆 5 点以后有空"  # the user's words, not the summary
    assert seen["artifacts"] == [str(tmp_path / "schedule.md")]  # a missing file is left out
    assert seen["txt_response"] == "最终答复" and seen["user_note"] == "外婆的时间要核对"
    assert result.signal_mode == "reward" and result.reward_overall.total_score == 72
    assert [s.tier for s in result.signals] == ["perfect", "weak", "fair", "fair", "uncertain"]
    assert result.signals[-1].signal_id == complaint.signal_id  # the user's complaint still reaches attribution
    assert result.run_meta["reward"]["engine"] == "fast_reward"
    assert result.reward_payload["schema_version"] == "attribution.reward.v1"
    assert [e.stage for e in events if getattr(e, "stage", "") == "reward"]


async def test_a_failed_fast_reward_falls_back_to_the_process_signals(monkeypatch: Any, tmp_path: Any) -> None:
    async def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ConnectionError("model unreachable")

    from openjiuwen.agent_evolving.fast_reward import pipeline

    monkeypatch.delenv("ATTRIBUTION_REWARD", raising=False)
    monkeypatch.setattr(pipeline, "reward", broken)
    monkeypatch.setattr(adapter, "fast_reward_config", lambda model: {})
    signal = DiagnosticSignal.create("错误", source=SignalSource.KEYWORD_TOOL, node_ids=[f"{LEADER}:1"])
    monkeypatch.setattr(run_module, "run_keyword_diagnosis", _keyword([signal]))
    result = await run_diagnosis(
        run_id="r",
        language="zh",
        nodes_by_subagent=_nodes(),
        user_attribution="",
        task_parse=_task_parse(tmp_path),
        model=object(),
    )
    assert result.signal_mode == "rule_jev" and result.signals == [signal]
    assert result.run_meta["reward"] == {
        "engine": "fast_reward",
        "failed": "ConnectionError",
        "error": "model unreachable",
    }


def test_fast_reward_is_the_default_and_the_reward_agent_stays_one_switch_away(monkeypatch: Any) -> None:
    monkeypatch.setenv("ATTRIBUTION_REWARD", "agent")
    assert adapter.fast_reward_enabled() is False
    monkeypatch.delenv("ATTRIBUTION_REWARD")
    assert adapter.fast_reward_enabled() is True
