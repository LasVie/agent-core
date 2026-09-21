# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from openjiuwen.core.context_engine.context.context_utils import ContextUtils
from openjiuwen.core.context_engine.context.react_cycle import removable_cycles
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage, UserMessage


def call(*ids):
    return AssistantMessage(
        content="", tool_calls=[ToolCall(id=value, type="function", name="read", arguments="{}") for value in ids]
    )


def test_parallel_cycle_and_current_user_protection():
    user = UserMessage(content="current input")
    messages = [
        user,
        call("a", "b"),
        ToolMessage(tool_call_id="b", content="B"),
        ToolMessage(tool_call_id="a", content="A"),
        AssistantMessage(content="latest"),
    ]
    ContextUtils.ensure_context_message_ids(messages)
    assert removable_cycles(messages, {user.metadata["context_message_id"]}) == [(1, 2, 3)]


def test_incomplete_calls_and_latest_cycle_stay():
    messages = [AssistantMessage(content="old"), call("a", "b"), ToolMessage(tool_call_id="a", content="A")]
    ContextUtils.ensure_context_message_ids(messages)
    assert removable_cycles(messages, set()) == []
    messages += [AssistantMessage(content="interleaved"), AssistantMessage(content="latest")]
    ContextUtils.ensure_context_message_ids(messages)
    assert removable_cycles(messages, set()) == [(0,)]


def test_missing_duplicate_and_orphan_tool_ids_are_not_archived():
    for ids, results in [(("",), [""]), (("a", "a"), ["a"]), (("a",), ["a", "other"])]:
        messages = [
            call(*ids),
            *[ToolMessage(tool_call_id=value, content="x") for value in results],
            AssistantMessage(content="latest"),
        ]
        ContextUtils.ensure_context_message_ids(messages)
        assert removable_cycles(messages, set()) == []


def test_replayed_id_does_not_protect_an_older_complete_occurrence():
    messages = [
        call("replayed"),
        ToolMessage(tool_call_id="replayed", content="first"),
        call("replayed"),
        ToolMessage(tool_call_id="replayed", content="second"),
    ]
    ContextUtils.ensure_context_message_ids(messages)
    assert removable_cycles(messages, set()) == [(0, 1)]


def test_malformed_old_cycle_does_not_disable_later_complete_cycles():
    messages = [
        call("old"),
        ToolMessage(tool_call_id="old", content="first result"),
        ToolMessage(tool_call_id="old", content="duplicate result"),
        UserMessage(content="resume"),
        call("old"),
        ToolMessage(tool_call_id="old", content="replayed result"),
        AssistantMessage(content="latest"),
    ]
    ContextUtils.ensure_context_message_ids(messages)
    assert removable_cycles(messages, set()) == [(3, 4, 5)]
