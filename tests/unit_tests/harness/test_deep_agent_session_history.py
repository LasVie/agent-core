# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real DeepAgent/ReAct tool loops with deterministic provider responses."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.context_engine import ContextEngineConfig, CycleArchiveProcessorConfig, SessionHistoryConfig
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    Model,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    ToolMessage,
)
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig, CheckpointerFactory
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail
from tests.unit_tests.fixtures.mock_llm import MockLLMModel


def make_agent(root):
    model = Model(
        ModelClientConfig(client_provider="OpenAI", api_key="fake", api_base="http://localhost:1"),
        ModelRequestConfig(model="test", max_tokens=128),
    )
    tool = LocalFunction(
        ToolCard(
            id="read_log",
            name="read_log",
            description="Read the sample log",
            input_params={"type": "object", "properties": {}},
        ),
        lambda: "log original " * 2000,
    )
    return create_deep_agent(
        model,
        card=AgentCard(id="persistent-agent"),
        system_prompt="Answer using the tools.",
        tools=[tool],
        workspace=str(root / "workspace"),
        enable_task_loop=False,
        add_general_purpose_agent=False,
        enable_task_planning=False,
        enable_skill_discovery=False,
        enable_tool_resilience_rail=False,
        enable_security_rail=False,
        enable_model_anomaly_detection_rail=False,
        enable_read_image_multimodal=False,
        rails=[ContextProcessorRail(preset=False, processors=("CycleArchiveProcessor", CycleArchiveProcessorConfig()))],
        context_engine_config=ContextEngineConfig(
            context_window_tokens=3000,
            session_history=SessionHistoryConfig(
                enabled=True, root_dir=str(root), output_reserve_tokens=128, safety_margin_tokens=64
            ),
        ),
    )


def responses():
    return [
        AssistantMessage(content="", tool_calls=[ToolCall(id="one", type="function", name="read_log", arguments="{}")]),
        AssistantMessage(content="", tool_calls=[ToolCall(id="two", type="function", name="read_log", arguments="{}")]),
        AssistantMessage(content="finished"),
    ]


def trajectory_records(root):
    import json

    return [
        [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        for path in root.rglob("trajectories/*.jsonl")
    ]


@pytest_asyncio.fixture(autouse=True)
async def runner(monkeypatch):
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", MagicMock())
    await Runner.start()
    yield
    await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_one_outer_execution_contains_all_tool_rounds(tmp_path, streaming):
    agent = make_agent(tmp_path)
    session = create_agent_session(session_id="session-a", card=agent.card)
    mock = MockLLMModel()
    mock.set_responses(responses())
    with patch.object(Model, "invoke", side_effect=mock.invoke), patch.object(Model, "stream", side_effect=mock.stream):
        if streaming:
            chunks = [chunk async for chunk in agent.stream({"query": "read two logs"}, session)]
            assert any(getattr(chunk, "type", None) == "answer" for chunk in chunks)
        else:
            assert (await agent.invoke({"query": "read two logs"}, session))["output"] == "finished"
    logs = trajectory_records(tmp_path)
    assert len(logs) == 1
    assert [record["message"]["role"] for record in logs[0]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert len({record["execution_id"] for record in logs[0]}) == 1
    assert all(record["occurred_at"] for record in logs[0])
    assert "log original " * 2000 in logs[0][2]["message"]["content"]
    assert list(tmp_path.rglob("offload/*.jsonl"))
    # Tool results feed the subsequent model request directly, as file references when oversized.
    assert any(
        getattr(message, "role", None) == "tool" and "OFFLOAD" in getattr(message, "content", "")
        for message in mock.call_history[1]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failure_or_cancel_exports_available_raw_messages(tmp_path, cancel):
    agent = make_agent(tmp_path)
    session = create_agent_session(session_id="failure", card=agent.card)

    async def fail(*args, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("provider failed")

    with patch.object(Model, "invoke", side_effect=fail):
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await agent.invoke({"query": "keep this input"}, session)
    logs = trajectory_records(tmp_path)
    assert len(logs) == 1
    assert logs[0][0]["message"]["content"] == "keep this input"
    assert session.get_state("context")


@pytest.mark.asyncio
async def test_save_failure_preserves_primary_exception(tmp_path):
    agent = make_agent(tmp_path)
    session = create_agent_session(session_id="both-fail", card=agent.card)
    with (
        patch.object(Model, "invoke", side_effect=RuntimeError("primary failure")),
        patch(
            "openjiuwen.core.context_engine.history.store.os.link",
            side_effect=OSError("disk full"),
        ),
    ):
        with pytest.raises(RuntimeError, match="primary failure") as captured:
            await agent.invoke({"query": "original"}, session)
    assert "ARCHIVE_FAILED" in str(captured.value.__cause__)
    assert any("save also failed" in note for note in captured.value.__notes__)


@pytest.mark.asyncio
async def test_inner_task_iterations_share_the_outer_execution(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    await agent._ensure_initialized()
    agent._deep_config.enable_task_loop = True
    session = create_agent_session(session_id="task-loop", card=agent.card)

    async def two_inner_iterations(ctx, bound_session):
        await agent.react_agent.invoke({"query": "first inner iteration"}, bound_session)
        return await agent.react_agent.invoke({"query": "second inner iteration"}, bound_session)

    monkeypatch.setattr(agent, "_run_task_loop_invoke", two_inner_iterations)
    with patch.object(Model, "invoke", return_value=AssistantMessage(content="done")):
        await agent.invoke({"query": "outer task"}, session)
    logs = trajectory_records(tmp_path)
    assert len(logs) == 1
    assert [record["seq"] for record in logs[0]] == [0, 1, 2, 3]
    assert len({record["execution_id"] for record in logs[0]}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workflow", "hitl"])
async def test_interrupt_resume_exports_both_outer_executions(tmp_path, monkeypatch, kind):
    from openjiuwen.core.single_agent.interrupt import InterruptRequest, ToolInterruptException
    from openjiuwen.core.workflow import WorkflowExecutionState, WorkflowOutput

    agent = make_agent(tmp_path)
    await agent._ensure_initialized()
    session = create_agent_session(session_id="interrupted-cycle", card=agent.card)
    original_execute = agent.react_agent.ability_manager.execute
    calls = 0

    async def execute(ctx, tool_call, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 and kind == "hitl":
            return [(ToolInterruptException(InterruptRequest(message="approve")), None)]
        result = (
            WorkflowOutput(result=None, state=WorkflowExecutionState.INPUT_REQUIRED)
            if calls == 1
            else "approved tool result"
        )
        return [(result, ToolMessage(tool_call_id="waiting", content=str(result)))]

    monkeypatch.setattr(agent.react_agent.ability_manager, "execute", execute)
    mock = MockLLMModel()
    mock.set_responses(
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="waiting", type="function", name="read_log", arguments="{}")],
            ),
            AssistantMessage(content="resumed"),
        ]
    )
    with patch.object(Model, "invoke", side_effect=mock.invoke):
        first = await agent.invoke({"query": "start"}, session)
        assert first["result_type"] == "interrupt"
        recorder = agent.react_agent.context_engine._history_recorder(session.get_session_id())
        assert recorder.last_execution.status == "interrupted"
        second = await agent.invoke({"query": "approved"}, session)
        assert second["output"] == "resumed"
        assert recorder.last_execution.status == "completed"

    logs = trajectory_records(tmp_path)
    assert len(logs) == 2
    records = [record for trajectory in logs for record in trajectory]
    assistants = [record for record in records if record["message"].get("tool_calls")]
    tools = [record for record in records if record["message"]["role"] == "tool"]
    assert calls == 2
    assert tools
    if kind == "hitl":
        assert len(assistants) == 1
        assert tools[0]["step_id"] == assistants[0]["step_id"]
    else:
        # Native workflow resume replays an assistant call; its new message is
        # a new cycle, while both executions preserve their original logs.
        assert len(assistants) == 2
        by_execution = {record["execution_id"]: record["step_id"] for record in assistants}
        assert all(record["step_id"] == by_execution[record["execution_id"]] for record in tools)

    # Archival must still work after native workflow replay has reused its IDs.
    monkeypatch.setattr(agent.react_agent.ability_manager, "execute", original_execute)
    mock.set_responses(responses())
    with patch.object(Model, "invoke", side_effect=mock.invoke):
        result = await agent.invoke({"query": "inspect more logs after resuming"}, session)
    assert result["output"] == "finished"
    assert list(tmp_path.rglob("offload/*.jsonl"))


@pytest.mark.asyncio
async def test_close_stream_exports_once_without_success_answer(tmp_path):
    agent = make_agent(tmp_path)
    session = create_agent_session(session_id="closing", card=agent.card)
    mock = MockLLMModel()
    mock.set_responses([AssistantMessage(content="streaming response")])
    with patch.object(Model, "stream", side_effect=mock.stream):
        # The native generic BaseAgent wrapper has a separate documented aclose
        # limitation; exercise DeepAgent's own outer lifecycle as its tests do.
        stream = DeepAgent.stream(agent, {"query": "input before closing"}, session)
        await anext(stream)
        await getattr(stream, "aclose")()
    logs = trajectory_records(tmp_path)
    assert len(logs) == 1
    assert logs[0][0]["message"]["content"] == "input before closing"


def test_minimal_agent_restores_in_a_separate_process(tmp_path):
    repository = Path(__file__).resolve().parents[3]
    script = repository / "examples" / "context_engine" / "session_cycle_archive.py"
    for phase in ("seed", "resume"):
        result = subprocess.run(
            [sys.executable, str(script), "--root", str(tmp_path), "--phase", phase],
            cwd=repository,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
        assert f"ACCEPTANCE {phase}: PASS" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_export", [False, True])
async def test_stream_checkpoint_waits_for_host_after_export(tmp_path, fail_export):
    database = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'state.db').as_posix()}")
    previous = CheckpointerFactory.get_checkpointer()
    checkpointer = await CheckpointerFactory.create(
        CheckpointerConfig(type="persistence", conf={"db_type": "sqlite", "db_client": database})
    )
    CheckpointerFactory.set_default_checkpointer(checkpointer)
    try:
        agent = make_agent(tmp_path)
        session = create_agent_session(session_id="stream-commit", card=agent.card)
        await session.pre_run()
        mock = MockLLMModel()
        mock.set_responses(responses())
        native_link = os.link

        def publish(source, target):
            if fail_export and "trajectories" in Path(target).parts:
                raise OSError("trajectory disk full")
            return native_link(source, target)

        chunks = []
        with (
            patch.object(session, "commit", wraps=session.commit) as commit,
            patch.object(Model, "stream", side_effect=mock.stream) as provider,
            patch("openjiuwen.core.context_engine.history.store.os.link", side_effect=publish),
        ):
            if fail_export:
                with pytest.raises(BaseError, match="ARCHIVE_FAILED"):
                    async for chunk in agent.stream({"query": "read two logs"}, session):
                        chunks.append(chunk)
                assert not any(getattr(chunk, "type", None) == "answer" for chunk in chunks)
            else:
                chunks = [chunk async for chunk in agent.stream({"query": "read two logs"}, session)]
                assert any(getattr(chunk, "type", None) == "answer" for chunk in chunks)
            commit.assert_not_awaited()
            assert provider.call_count == 3

        restored = create_agent_session(session_id="stream-commit", card=agent.card)
        await restored.pre_run()
        assert not restored.get_state("context"), "inner stream committed before the host's save"

        engine = agent.react_agent.context_engine
        if fail_export:
            assert not list(tmp_path.rglob("trajectories/*.jsonl"))
            with pytest.raises(BaseError, match="already active"):
                await agent.invoke({"query": "must not overwrite unsaved execution"}, session)
        # Recovery only exports/snapshots; it does not call the model or tools.
        record = await engine.finish_history_execution(session)
        assert record.status == "completed"
        assert await engine.finish_history_execution(session) == record
        assert len(trajectory_records(tmp_path)) == 1
        assert len(trajectory_records(tmp_path)[0]) == 6
        await session.commit()
        restored = create_agent_session(session_id="stream-commit", card=agent.card)
        await restored.pre_run()
        state = restored.get_state("context")["default_context_id"]
        assert state["session_history"]["last_execution"]["execution_id"] == record.execution_id
        assert state["messages"][-1].content == "finished"
    finally:
        CheckpointerFactory.set_default_checkpointer(previous)
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_aborted_parallel_calls_preserve_results_and_allow_next_request(tmp_path, cancel):
    agent = make_agent(tmp_path)
    await agent._ensure_initialized()
    session = create_agent_session(session_id="aborted-tools", card=agent.card)
    calls = [ToolCall(id=value, type="function", name="read_log", arguments="{}") for value in ("done", "pending")]
    assistant = AssistantMessage(content="original assistant", tool_calls=calls)

    async def abort_after_partial_result(ctx, **kwargs):
        await ctx.context.add_messages(ToolMessage(tool_call_id="done", content="original successful result"))
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("tool execution failed")

    with (
        patch.object(Model, "invoke", return_value=assistant),
        patch.object(agent.react_agent.ability_manager, "execute", side_effect=abort_after_partial_result),
    ):
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await agent.invoke({"query": "start two tools"}, session)
    records = trajectory_records(tmp_path)[0]
    assert [record["message"]["role"] for record in records] == ["user", "assistant", "tool", "tool"]
    assert records[1]["message"]["content"] == "original assistant"
    assert records[2]["message"]["content"] == "original successful result"
    assert records[3]["message"]["tool_call_id"] == "pending"
    assert "execution was aborted" in records[3]["message"]["content"]
    assert records[3]["step_id"] == records[1]["step_id"]

    async def verify_request(messages, **kwargs):
        pending: set[str] = set()
        for message in messages:
            if message.role in {"assistant", "user"}:
                assert not pending, "a new message followed an unanswered tool call"
            if isinstance(message, AssistantMessage):
                pending.update(call.id for call in message.tool_calls or [])
            elif isinstance(message, ToolMessage):
                assert message.tool_call_id in pending
                pending.remove(message.tool_call_id)
        assert not pending
        return AssistantMessage(content="next request completed")

    with patch.object(Model, "invoke", side_effect=verify_request):
        result = await agent.invoke({"query": "continue with new work"}, session)
    assert result["output"] == "next request completed"
