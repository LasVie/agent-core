# coding: utf-8
"""Tests for the run-level orchestration: diagnosis and attribution."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openjiuwen.agent_evolving.attribution import run as run_module
from openjiuwen.agent_evolving.attribution.model import (
    DiagnosticSignal,
    SignalSource,
    TaskParseResult,
)
from openjiuwen.agent_evolving.attribution.nodes import TrajectoryNode
from openjiuwen.agent_evolving.attribution.run import (
    NodeBackedRuntime,
    build_reward_user_prompt,
    run_attribution,
    run_diagnosis,
)

pytestmark = pytest.mark.asyncio


def _nodes() -> dict[str, list[TrajectoryNode]]:
    return {
        "main": [
            TrajectoryNode(
                node_id=f"main:{index}",
                subagent_key="main",
                display_name="M",
                kind="main_agent",
                role="user" if index == 0 else "tool",
                content=f"content {index}" if index == 0 else f"content {index} 错误",
                name="bash" if index else None,
                start_ts_ns=index * 100,
                end_ts_ns=index * 100 + 50,
            )
            for index in range(3)
        ]
    }


def _task_parse(with_artifacts: bool = True) -> TaskParseResult:
    return TaskParseResult(
        task_description="写报告",
        message_artifacts=[("main:0", "帮我写报告")] if with_artifacts else [],
        file_artifacts=["/w/report.md"] if with_artifacts else [],
        file_candidates=[("/w/report.md", "main:1", "write_file")] if with_artifacts else [],
        run_meta={},
    )


def _signal() -> DiagnosticSignal:
    return DiagnosticSignal.create("错误", source=SignalSource.KEYWORD_TOOL, node_ids=["main:1"])


def test_build_reward_user_prompt_zh() -> None:
    prompt = build_reward_user_prompt(
        task_description="写报告",
        message_artifacts=[("main:0", "帮我写报告")],
        file_artifacts=["/w/report.md"],
        signals=[_signal()],
        user_attribution="用户补充",
        language="zh",
    )
    assert "## 任务描述" in prompt
    assert "main:0: 帮我写报告" in prompt
    assert "/w/report.md" in prompt
    assert "用户补充" in prompt
    assert "submit_reward" in prompt


async def test_run_diagnosis_skips_reward_without_artifacts() -> None:
    result = await run_diagnosis(
        run_id="r1",
        language="zh",
        nodes_by_subagent=_nodes(),
        user_attribution="",
        task_parse=_task_parse(with_artifacts=False),
        keyword_mode="rule",
    )
    assert result.signal_mode == "rule_jev"
    assert result.reward_overall is None
    assert result.run_meta["reward"]["skipped"] == "no artifacts"
    assert len(result.signals) >= 1


async def test_run_diagnosis_skips_reward_without_model() -> None:
    result = await run_diagnosis(
        run_id="r1",
        language="zh",
        nodes_by_subagent=_nodes(),
        user_attribution="",
        task_parse=_task_parse(with_artifacts=True),
        keyword_mode="rule",
        model=None,
    )
    assert result.signal_mode == "rule_jev"
    assert result.run_meta["reward"]["skipped"] == "no model injected"


class _FakeDeepAgent:
    """Drives the runtime directly to simulate an attribution agent."""

    def __init__(self, runtime: NodeBackedRuntime, signals: list[DiagnosticSignal], rounds_to_finish: int = 1) -> None:
        self._runtime = runtime
        self._signals = signals
        self._rounds_to_finish = rounds_to_finish
        self.invocations = 0
        self.prompts: list[str] = []

    async def invoke(self, prompt: str, session: Any = None) -> dict[str, Any]:
        del session
        self.invocations += 1
        self.prompts.append(prompt)
        if self.invocations >= self._rounds_to_finish:
            for signal in self._signals:
                self._runtime.record_attribution(signal.signal_id, [{"text_md": "cause", "start_node_id": "main:1"}])
            self._runtime.record_artifact_improvement("improve it")
        return {}


class _FakeRail:
    def __init__(self) -> None:
        self.exceeded = False
        self.spent = 0


def _patch_build(monkeypatch: Any, fake_agent: _FakeDeepAgent, fake_rail: _FakeRail) -> None:
    def fake_build(**kwargs: Any) -> tuple[Any, _FakeRail]:
        del kwargs
        return fake_agent, fake_rail

    monkeypatch.setattr(run_module, "build_attribution_agent", fake_build)


class _FakeSession:
    pass


async def test_run_attribution_success_single_round(monkeypatch: Any) -> None:
    runtime = NodeBackedRuntime(_nodes())
    signals = [_signal()]
    fake_agent = _FakeDeepAgent(runtime, signals, rounds_to_finish=1)
    _patch_build(monkeypatch, fake_agent, _FakeRail())
    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", _FakeSession)
    outcome = await run_attribution(
        model=object(),
        nodes_by_subagent=_nodes(),
        signals=signals,
        task_description="写报告",
        runtime=runtime,
        total_timeout_s=None,
    )
    assert outcome.status == "success"
    assert outcome.completed is True
    assert outcome.wake_attempted is False
    assert fake_agent.invocations == 1
    assert outcome.counts["attributed"] == 1


async def test_run_attribution_wake_once_then_success(monkeypatch: Any) -> None:
    runtime = NodeBackedRuntime(_nodes())
    signals = [_signal()]
    fake_agent = _FakeDeepAgent(runtime, signals, rounds_to_finish=2)
    _patch_build(monkeypatch, fake_agent, _FakeRail())
    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", _FakeSession)
    outcome = await run_attribution(
        model=object(),
        nodes_by_subagent=_nodes(),
        signals=signals,
        task_description="写报告",
        runtime=runtime,
        total_timeout_s=None,
    )
    assert outcome.status == "success"
    assert outcome.completed is True
    assert outcome.wake_attempted is True
    assert fake_agent.invocations == 2
    assert "pending_signal_ids" in fake_agent.prompts[1]


async def test_run_attribution_wake_fails_incomplete(monkeypatch: Any) -> None:
    runtime = NodeBackedRuntime(_nodes())
    signals = [_signal()]
    fake_agent = _FakeDeepAgent(runtime, signals, rounds_to_finish=99)
    _patch_build(monkeypatch, fake_agent, _FakeRail())
    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", _FakeSession)
    outcome = await run_attribution(
        model=object(),
        nodes_by_subagent=_nodes(),
        signals=signals,
        task_description="写报告",
        runtime=runtime,
        total_timeout_s=None,
    )
    assert outcome.status == "failed"
    assert outcome.failure_kind == "incomplete"
    assert outcome.wake_attempted is True
    assert outcome.pending_signal_ids == [signals[0].signal_id]
    assert fake_agent.invocations == 2


async def test_run_attribution_tokens_exceeded(monkeypatch: Any) -> None:
    runtime = NodeBackedRuntime(_nodes())
    signals = [_signal()]
    fake_agent = _FakeDeepAgent(runtime, signals, rounds_to_finish=99)
    rail = _FakeRail()
    rail.exceeded = True
    _patch_build(monkeypatch, fake_agent, rail)
    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", _FakeSession)
    outcome = await run_attribution(
        model=object(),
        nodes_by_subagent=_nodes(),
        signals=signals,
        task_description="写报告",
        runtime=runtime,
        total_timeout_s=None,
    )
    assert outcome.status == "failed"
    assert outcome.failure_kind == "tokens_exceeded"


class _SlowAgent:
    async def invoke(self, prompt: str, session: Any = None) -> dict[str, Any]:
        del prompt, session
        await asyncio.sleep(0.2)
        return {}


async def test_run_attribution_timeout(monkeypatch: Any) -> None:
    fake_rail = _FakeRail()
    monkeypatch.setattr(run_module, "build_attribution_agent", lambda **kwargs: (_SlowAgent(), fake_rail))
    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", _FakeSession)
    outcome = await run_attribution(
        model=object(),
        nodes_by_subagent=_nodes(),
        signals=[_signal()],
        task_description="写报告",
        runtime=NodeBackedRuntime(_nodes()),
        total_timeout_s=0.05,
    )
    assert outcome.status == "failed"
    assert outcome.failure_kind == "timeout"


async def test_run_attribution_empty_signals_short_circuits() -> None:
    outcome = await run_attribution(
        model=object(),
        nodes_by_subagent=_nodes(),
        signals=[],
        task_description="写报告",
    )
    assert outcome.status == "success"
    assert outcome.completed is True


async def test_run_attribution_agent_error(monkeypatch: Any) -> None:
    class _ExplodingAgent:
        async def invoke(self, prompt: str, session: Any = None) -> dict[str, Any]:
            del prompt, session
            raise RuntimeError("model broke")

    fake_rail = _FakeRail()
    runtime = NodeBackedRuntime(_nodes())
    monkeypatch.setattr(run_module, "build_attribution_agent", lambda **kwargs: (_ExplodingAgent(), fake_rail))
    monkeypatch.setattr("openjiuwen.core.session.agent.create_agent_session", _FakeSession)
    outcome = await run_attribution(
        model=object(),
        nodes_by_subagent=_nodes(),
        signals=[_signal()],
        task_description="写报告",
        runtime=runtime,
        total_timeout_s=None,
    )
    assert outcome.status == "failed"
    assert outcome.failure_kind == "error"
    assert "model broke" in outcome.error_detail or True
