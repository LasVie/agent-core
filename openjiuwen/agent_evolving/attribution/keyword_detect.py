# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Stage two (keyword part): negative-feedback detection over trajectory nodes.

Two modes share the same traversal: the deterministic rule mode scans tool
and user content against one merged zh+en negative-feedback word set with
negation suppression and density top-N, while the Jev mode asks the decision
model once per candidate node.  Jev unavailability degrades to rule mode.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from openjiuwen.agent_evolving.attribution.model import DiagnosticSignal, SignalSource, jev_tier
from openjiuwen.agent_evolving.attribution.nodes import TrajectoryNode

if TYPE_CHECKING:
    from openjiuwen.agent_evolving.attribution.jev import JevClient

_NEGATION_WORDS: tuple[str, ...] = (
    "不是",
    "不会",
    "不能",
    "没有",
    "不再",
    "无法",
    "未能",
    "不",
    "没",
    "无",
    "未",
    "非",
    "not",
    "no",
    "without",
    "never",
    "neither",
    "nor",
    "none",
    "isn't",
    "aren't",
    "wasn't",
    "weren't",
    "hasn't",
    "haven't",
    "hadn't",
    "don't",
    "doesn't",
    "didn't",
    "won't",
    "can't",
    "cannot",
    "couldn't",
    "shouldn't",
    "wouldn't",
    "mustn't",
    "shan't",
    "needn't",
)

_NEGATIVE_FEEDBACK_WORDS: tuple[str, ...] = (
    "错误",
    "异常",
    "失败",
    "超时",
    "报错",
    "出错",
    "有错",
    "错了",
    "搞错",
    "这不对",
    "不对",
    "不是这",
    "不是那",
    "理解错",
    "纠正",
    "我的意思是",
    "应该是",
    "应该用",
    "应该改",
    "应该换",
    "重新来",
    "重新做",
    "重新执行",
    "重新尝试",
    "有问题",
    "出问题",
    "故障",
    "缺陷",
    "崩溃",
    "崩了",
    "挂了",
    "不行",
    "不满足",
    "不通过",
    "未通过",
    "不生效",
    "无效",
    "丢失",
    "损坏",
    "卡死",
    "卡住",
    "死循环",
    "没反应",
    "无响应",
    "越界",
    "空指针",
    "内存溢出",
    "拒绝",
    "不好",
    "不够好",
    "error",
    "exception",
    "traceback",
    "failed",
    "failure",
    "timeout",
    "timed out",
    "errno",
    "connectionerror",
    "oserror",
    "valueerror",
    "typeerror",
    "no such file",
    "permission denied",
    "access denied",
    "command not found",
    "not recognized",
    "module not found",
    "econnrefused",
    "econnreset",
    "enoent",
    "enotfound",
    "npm err!",
    "wrong",
    "incorrect",
    "not right",
    "you're wrong",
    "should be",
    "should use",
    "should have",
    "correction",
    "fixed",
    "bug",
    "broken",
    "crash",
    "crashed",
    "malfunction",
    "invalid",
    "unable",
    "doesn't work",
    "does not work",
    "not working",
    "mismatch",
    "missing",
    "not found",
    "null pointer",
    "segfault",
    "core dumped",
    "killed",
    "out of memory",
    "hung",
    "deadlock",
    "corrupted",
    "aborted",
    "stack overflow",
    "index out of range",
    "key error",
    "attribute error",
    "import error",
    "refused",
    "denied",
    "rejected",
)

_DATA_FETCH_TOOLS = frozenset(
    {
        "mcp_fetch_webpage",
        "fetch_webpage",
        "web_fetch",
        "search",
        "web_search",
        "google_search",
        "bing_search",
        "view_file",
        "read_file",
        "cat_file",
        "list_directory",
        "ls",
        "get_url",
        "curl",
        "wget",
        "ttse_consult",
    }
)

_TOOL_SCHEMA_PATTERN = re.compile(r"\{'content': '---\\nname: [^\n]+\\ndescription:")

_EXCERPT_BEFORE = 300
_EXCERPT_AFTER = 300
_JEV_TEXT_LIMIT = 1024
_JEV_THRESHOLD = 0.8


def _normalize_apostrophes(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def _word_alternation(words: tuple[str, ...]) -> str:
    parts: list[str] = []
    for word in sorted(set(words), key=len, reverse=True):
        escaped = re.escape(word)
        parts.append(rf"\b(?:{escaped})\b" if word.isascii() else rf"(?:{escaped})")
    return "|".join(parts)


_NEGATIVE_WORD_PATTERN = re.compile(_word_alternation(_NEGATIVE_FEEDBACK_WORDS), re.IGNORECASE)
_NEGATION_END_PATTERN = re.compile(rf"(?:{_word_alternation(_NEGATION_WORDS)})\Z", re.IGNORECASE)


def _is_negated(content: str, start: int) -> bool:
    k = start
    spaces = 0
    while k > 0 and content[k - 1] == " " and spaces < 2:
        k -= 1
        spaces += 1
    if k == 0:
        return False
    return _NEGATION_END_PATTERN.search(content[:k]) is not None


def _excerpt_around(content: str, start: int, end: int) -> str:
    return content[max(0, start - _EXCERPT_BEFORE) : min(len(content), end + _EXCERPT_AFTER)]


def _scan_matches(node: TrajectoryNode) -> list[tuple[int, int]] | None:
    role = node.role
    content = _normalize_apostrophes(str(node.content or ""))
    if not content:
        return None
    if role == "tool":
        if str(node.name or "").lower() in _DATA_FETCH_TOOLS:
            return None
        if _TOOL_SCHEMA_PATTERN.search(content):
            return None
    elif role != "user":
        return None
    matches: list[tuple[int, int]] = []
    for match in _NEGATIVE_WORD_PATTERN.finditer(content):
        if _is_negated(content, match.start()):
            continue
        matches.append(match.span())
    return matches or None


def _source_for_role(role: str) -> SignalSource:
    return SignalSource.KEYWORD_TOOL if role == "tool" else SignalSource.KEYWORD_USER


def _rule_signal(node: TrajectoryNode, matches: list[tuple[int, int]]) -> DiagnosticSignal:
    content = _normalize_apostrophes(str(node.content or ""))
    start, end = matches[0]
    return DiagnosticSignal.create(
        _excerpt_around(content, start, end),
        source=_source_for_role(node.role),
        node_ids=[node.node_id],
        ts_ns=node.start_ts_ns,
        message_id=node.message_id,
    )


def detect_rule_signals(nodes_by_subagent: Mapping[str, list[TrajectoryNode]]) -> list[DiagnosticSignal]:
    """Run the deterministic keyword rules with density top-N ranking.

    ``k = min(max(3, N // 3), 10, B)`` where ``N`` is the tool+user node
    count and ``B`` the count of nodes with at least one non-negated match.
    Candidates rank by matched-character density; the kept signals return in
    subagent-name + node-time order.
    """
    candidates: list[tuple[float, int, TrajectoryNode, list[tuple[int, int]]]] = []
    node_count = 0
    position = 0
    for subagent in sorted(nodes_by_subagent):
        for node in nodes_by_subagent[subagent]:
            if node.role in ("tool", "user"):
                node_count += 1
            matches = _scan_matches(node)
            if not matches:
                continue
            content = _normalize_apostrophes(str(node.content or ""))
            matched_chars = sum(end - start for start, end in matches)
            density = matched_chars / len(content) if content else 0.0
            candidates.append((density, position, node, matches))
            position += 1
    if not candidates:
        return []
    keep = min(max(3, node_count // 3), 10, len(candidates))
    candidates.sort(key=lambda entry: (-entry[0], entry[1]))
    chosen = sorted(candidates[:keep], key=lambda entry: entry[1])
    return [_rule_signal(node, matches) for _, _, node, matches in chosen]


def _node_text_for_jev(node: TrajectoryNode, limit: int = _JEV_TEXT_LIMIT) -> str:
    role = node.role
    content = str(node.content or "")
    if role == "tool":
        name = str(node.name or "").strip()
        text = f"{name}: {content}" if name else content
    elif content:
        text = f"{role}: {content}"
    else:
        text = role
    return text[:limit]


async def detect_jev_signals(
    nodes_by_subagent: Mapping[str, list[TrajectoryNode]],
    client: "JevClient",
    *,
    threshold: float = _JEV_THRESHOLD,
    concurrency: int = 8,
) -> list[DiagnosticSignal]:
    """Ask Jev once per tool/user node and emit signals at ``P >= threshold``."""
    flat: list[TrajectoryNode] = []
    for subagent in sorted(nodes_by_subagent):
        for node in nodes_by_subagent[subagent]:
            if node.role in ("tool", "user"):
                if node.role == "tool":
                    if str(node.name or "").lower() in _DATA_FETCH_TOOLS:
                        continue
                    if _TOOL_SCHEMA_PATTERN.search(str(node.content or "")):
                        continue
                flat.append(node)

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def judge(node: TrajectoryNode) -> DiagnosticSignal | None:
        text = _node_text_for_jev(node)
        if not text:
            return None
        async with semaphore:
            answers = await client.system_one(
                {"category": node.role, "content": text},
                {
                    "is_diagnostic": {
                        "type": "noul",
                        "instructions": "该 span 是否包含负反馈、诊断信号或故障信号（错误、纠正、异常等）？",
                    }
                },
            )
        answer = answers.get("is_diagnostic") or {}
        try:
            probability = float(answer.get("noul") or 0.0)
        except (TypeError, ValueError):
            probability = 0.0
        if probability < threshold:
            return None
        return DiagnosticSignal.create(
            str(node.content or ""),
            source=_source_for_role(node.role),
            tier=jev_tier(probability * 100.0),
            node_ids=[node.node_id],
            ts_ns=node.start_ts_ns,
            message_id=node.message_id,
            confidence=probability,
        )

    results = await asyncio.gather(*(judge(node) for node in flat))
    return [signal for signal in results if signal is not None]


async def run_keyword_diagnosis(
    nodes_by_subagent: Mapping[str, list[TrajectoryNode]],
    mode: str,
    jev_client: "JevClient | None" = None,
    *,
    jev_threshold: float = _JEV_THRESHOLD,
    jev_concurrency: int = 8,
) -> tuple[list[DiagnosticSignal], dict[str, Any]]:
    """Run keyword diagnosis; a Jev failure degrades to rule mode.

    Returns ``(signals, run_meta)`` with the effective mode recorded.
    """
    if str(mode).strip().lower() != "jev":
        signals = detect_rule_signals(nodes_by_subagent)
        return signals, {"mode": "rule", "count": len(signals)}
    if jev_client is None:
        signals = detect_rule_signals(nodes_by_subagent)
        return signals, {"mode": "rule", "count": len(signals), "degraded": "jev_client_missing"}
    try:
        signals = await detect_jev_signals(
            nodes_by_subagent, jev_client, threshold=jev_threshold, concurrency=jev_concurrency
        )
        return signals, {"mode": "jev", "count": len(signals)}
    except Exception as exc:  # noqa: BLE001 - degrade on any Jev failure
        signals = detect_rule_signals(nodes_by_subagent)
        return signals, {"mode": "rule", "count": len(signals), "degraded": type(exc).__name__}


__all__ = [
    "detect_jev_signals",
    "detect_rule_signals",
    "run_keyword_diagnosis",
]
