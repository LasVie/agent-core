# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Conservative archive boundaries built on the native ReAct grouping helpers."""

from openjiuwen.core.context_engine.context.session_memory_manager import group_completed_api_rounds
from openjiuwen.core.foundation.llm import AssistantMessage, BaseMessage, ToolMessage


def completed_cycle_ranges(messages: list[BaseMessage]) -> list[tuple[int, int]]:
    """Validate native completed ranges without accepting malformed tool pairs."""
    complete = []
    for start, end in group_completed_api_rounds(messages):
        # Native grouping closes at the last expected result. Adjacent duplicate
        # or orphan results make this occurrence malformed, not every later one.
        while end < len(messages) and isinstance(messages[end], ToolMessage):
            end += 1
        assistants = [m for m in messages[start:end] if isinstance(m, AssistantMessage)]
        if len(assistants) != 1:
            continue
        calls = [call.id for call in assistants[0].tool_calls or []]
        results = [m.tool_call_id for m in messages[start:end] if isinstance(m, ToolMessage)]
        if len(set(calls)) != len(calls) or any(not call for call in calls):
            continue
        if sorted(calls) == sorted(results):
            complete.append((start, end))
    return complete


def removable_cycles(messages: list[BaseMessage], protected_ids: set[str]) -> list[tuple[int, ...]]:
    """Return oldest complete cycles, excluding the latest cycle and protected input.

    Malformed/interleaved calls are retained, never inferred to be complete.
    The native first range can include a user input; only unprotected members
    of that range are candidates for removal.
    """
    complete = completed_cycle_ranges(messages)
    if not complete:
        return []
    # Every accepted range already contains its complete call/result occurrence.
    # Matching IDs across the whole suffix would bind a replay to an older call
    # with the same ID and incorrectly protect all intervening complete cycles.
    protected_start = complete[-1][0]
    candidates = []
    for start, end in complete[:-1]:
        if end > protected_start:
            continue
        indexes = tuple(
            i
            for i in range(start, end)
            if messages[i].metadata.get("context_message_id") not in protected_ids and messages[i].role != "system"
        )
        if indexes:
            candidates.append(indexes)
    return candidates
