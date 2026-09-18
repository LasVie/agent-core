# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from openjiuwen.core.context_engine.history.recorder import SessionHistoryRecorder
from openjiuwen.core.context_engine.history.store import SessionHistoryStore
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, ToolMessage, UserMessage


def test_originals_survive_mutation_and_execution_export(tmp_path):
    recorder = SessionHistoryRecorder(SessionHistoryStore(str(tmp_path), "s"))
    execution = recorder.begin()
    user = UserMessage(content="question")
    assistant = AssistantMessage(
        content="working", tool_calls=[ToolCall(id="tool", type="function", name="read", arguments="{}")]
    )
    tool = ToolMessage(content="original", tool_call_id="tool")
    recorder.capture([user, assistant, tool])
    tool.content = "rewritten"
    records = recorder.current_records()
    assert records[-1].message["content"] == "original"
    assert records[-1].step_id == records[-2].step_id
    assert [r.seq for r in records] == [0, 1, 2]
    assert recorder.protected_user_ids() == {user.metadata["context_message_id"]}
    assert recorder.finish().execution_id == execution


def test_restore_does_not_retimestamp_or_rerecord(tmp_path):
    store = SessionHistoryStore(str(tmp_path), "s")
    first = SessionHistoryRecorder(store)
    first.begin()
    message = UserMessage(content="old")
    first.capture([message])
    original = first.current_records()[0]
    first.finish()
    state = first.snapshot([message])
    second = SessionHistoryRecorder(store)
    second.begin()
    second.restore(state)
    second.capture([message])
    assert second.current_records() == []
    assert second.originals([message])[0] == original
    legacy = UserMessage(content="legacy")
    assert second.originals([legacy])[0].occurred_at is None
