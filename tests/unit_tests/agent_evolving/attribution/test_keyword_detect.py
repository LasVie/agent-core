# coding: utf-8
"""Tests for keyword diagnosis (rule and Jev modes) and signal models."""

from __future__ import annotations

from typing import Any

import pytest

from openjiuwen.agent_evolving.attribution.keyword_detect import (
    detect_rule_signals,
    run_keyword_diagnosis,
)
from openjiuwen.agent_evolving.attribution.model import (
    DiagnosticSignal,
    SignalSource,
    jev_tier,
    make_signal_id,
    reward_tier,
)
from openjiuwen.agent_evolving.attribution.nodes import TrajectoryNode

pytestmark = pytest.mark.asyncio


def _node(index: int, role: str, content: str, name: str | None = None, subagent: str = "main") -> TrajectoryNode:
    return TrajectoryNode(
        node_id=f"{subagent}:{index}",
        subagent_key=subagent,
        display_name="M",
        kind="main_agent",
        role=role,
        content=content,
        name=name,
        start_ts_ns=index * 100,
        end_ts_ns=index * 100 + 50,
    )


def test_rule_signal_negation_suppression() -> None:
    nodes = {
        "main": [
            _node(0, "user", "这个结果不对，出错了"),
            _node(1, "user", "没有错误，一切正常"),
            _node(2, "user", "not wrong, all good"),
            _node(3, "user", "cannot fail 这个设计"),
            _node(4, "user", "won't fail"),
        ]
    }
    signals = detect_rule_signals(nodes)
    assert [signal.node_ids[0] for signal in signals] == ["main:0"]


def test_rule_signal_data_fetch_and_schema_dump_skipped() -> None:
    nodes = {
        "main": [
            _node(0, "tool", "web content with an error in it", name="web_fetch"),
            _node(1, "tool", "{'content': '---\\nname: tool\\ndescription: does error stuff'}", name="mcp__tools"),
            _node(2, "tool", "error in my script", name="bash"),
        ]
    }
    signals = detect_rule_signals(nodes)
    assert [signal.node_ids[0] for signal in signals] == ["main:2"]


def test_rule_signal_top_k_density_and_order() -> None:
    nodes = {
        "a": [_node(0, "tool", "错误 错误 错误 错误", name="bash", subagent="a")],
        "b": [_node(0, "tool", "错误", name="bash", subagent="b")],
        "c": [_node(0, "tool", "something 失败 here", name="bash", subagent="c")],
    }
    signals = detect_rule_signals(nodes)
    assert signals[0].node_ids[0] == "a:0"


def test_rule_signal_excerpt_and_fields() -> None:
    content = "x" * 50 + "发生了错误" + "y" * 50
    nodes = {"main": [_node(0, "tool", content, name="bash")]}
    signals = detect_rule_signals(nodes)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.source == SignalSource.KEYWORD_TOOL
    assert "发生了错误" in signal.text
    assert signal.tier == "uncertain"
    assert signal.node_ids == ["main:0"]
    assert signal.message_id is None


class _FakeJevClient:
    def __init__(self, probabilities: list[float], fail: bool = False) -> None:
        self._probabilities = probabilities
        self._fail = fail
        self.calls = 0

    async def system_one(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        del state, questions
        self.calls += 1
        if self._fail:
            raise ConnectionError("jev down")
        probability = self._probabilities[min(self.calls - 1, len(self._probabilities) - 1)]
        return {"is_diagnostic": {"noul": probability}}


async def test_jev_signals_threshold_and_tier() -> None:
    nodes = {"main": [_node(0, "tool", "boom", name="bash"), _node(1, "user", "meh")]}
    client = _FakeJevClient([0.95, 0.1])
    signals, meta = await run_keyword_diagnosis(nodes, "jev", jev_client=client)
    assert meta["mode"] == "jev"
    assert len(signals) == 1
    assert signals[0].tier == "weak"
    assert signals[0].confidence == 0.95


async def test_jev_unavailable_degrades_to_rule() -> None:
    nodes = {"main": [_node(0, "tool", "error happened", name="bash")]}
    client = _FakeJevClient([0.95], fail=True)
    signals, meta = await run_keyword_diagnosis(nodes, "jev", jev_client=client)
    assert meta["mode"] == "rule"
    assert meta["degraded"] == "ConnectionError"
    assert len(signals) == 1


async def test_jev_missing_client_degrades() -> None:
    nodes = {"main": [_node(0, "tool", "AssertionError", name="bash")]}
    signals, meta = await run_keyword_diagnosis(nodes, "jev", jev_client=None)
    assert meta["mode"] == "rule"
    assert meta["degraded"] == "jev_client_missing"


def test_tier_mappings() -> None:
    assert jev_tier(95.0) == "weak"
    assert jev_tier(90.0) == "weak"
    assert jev_tier(80.0) == "uncertain"
    assert jev_tier(70.0) == "uncertain"
    assert jev_tier(50.0) == "fair"
    assert reward_tier(100.0) == "perfect"
    assert reward_tier(90.0) == "perfect"
    assert reward_tier(89.0) == "fair"
    assert reward_tier(75.0) == "fair"
    assert reward_tier(74.0) == "weak"


def test_signal_id_stable_and_roundtrip() -> None:
    signal = DiagnosticSignal.create(
        "same text",
        source=SignalSource.KEYWORD_TOOL,
        node_ids=["main:1"],
        ts_ns=123,
        tier="uncertain",
    )
    again = DiagnosticSignal.create("same text", source=SignalSource.KEYWORD_TOOL, ts_ns=123)
    assert signal.signal_id == again.signal_id == make_signal_id("same text", SignalSource.KEYWORD_TOOL, 123)
    assert signal.signal_id.startswith("diag-")
    assert len(signal.signal_id) == len("diag-") + 24
    restored = DiagnosticSignal.from_dict(signal.to_dict())
    assert restored.to_dict() == signal.to_dict()


@pytest.mark.parametrize("value,expected", [(100.0, "perfect"), (89.9, "fair"), (0.0, "weak")])
def test_reward_tier_bands(value: float, expected: str) -> None:
    assert reward_tier(value) == expected
