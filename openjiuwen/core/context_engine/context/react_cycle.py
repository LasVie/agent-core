# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Conservative archive boundaries built on the native ReAct grouping helpers."""

from openjiuwen.core.context_engine.context.session_memory_manager import group_completed_api_rounds
from openjiuwen.core.foundation.llm import AssistantMessage, BaseMessage, ToolMessage


def removable_cycles(messages: list[BaseMessage], protected_ids: set[str]) -> list[tuple[int, ...]]:
    """Return oldest complete cycles, excluding the latest cycle and protected input.

    Malformed/interleaved calls are retained, never inferred to be complete.
    The native first range can include a user input; only unprotected members
    of that range are candidates for removal.
    """
    # Lazy import: the forked processor subtree imports the context engine.
    from openjiuwen.core.context_engine.processor.forked.compressor.base import (
        adjust_keep_recent_for_tool_boundaries,
    )

    known_calls = [
        call.id for message in messages if isinstance(message, AssistantMessage) for call in message.tool_calls or []
    ]
    result_ids = [message.tool_call_id for message in messages if isinstance(message, ToolMessage)]
    if len(set(known_calls)) != len(known_calls) or len(set(result_ids)) != len(result_ids):
        return []
    if any(value not in known_calls for value in result_ids):
        return []
    complete = []
    for start, end in group_completed_api_rounds(messages):
        assistants = [m for m in messages[start:end] if isinstance(m, AssistantMessage)]
        if len(assistants) != 1:
            continue
        calls = [call.id for call in assistants[0].tool_calls or []]
        results = [m.tool_call_id for m in messages[start:end] if isinstance(m, ToolMessage)]
        if len(set(calls)) != len(calls) or any(not call for call in calls):
            continue
        if sorted(calls) == sorted(results):
            complete.append((start, end))
    if not complete:
        return []
    keep_recent = adjust_keep_recent_for_tool_boundaries(messages, len(messages) - complete[-1][0])
    protected_start = len(messages) - keep_recent
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
