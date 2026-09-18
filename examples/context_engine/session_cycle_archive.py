# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Offline persistent DeepAgent acceptance, using real tools and native checkpoints.

Run seed and resume in separate processes against an empty host-owned directory:
    python examples/context_engine/session_cycle_archive.py --root C:/Temp/agent-demo --phase seed
    python examples/context_engine/session_cycle_archive.py --root C:/Temp/agent-demo --phase resume

Only the model response is scripted (no API key/network required). DeepAgent,
ReAct, tool execution, ContextEngine, JSONL history and SQLite are the real SDK.
Use a fresh directory for a new acceptance run. Session writers are sequential.
"""

import argparse
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import anyio
from sqlalchemy.ext.asyncio import create_async_engine

from openjiuwen.core.context_engine import ContextEngineConfig, CycleArchiveProcessorConfig, SessionHistoryConfig
from openjiuwen.core.foundation.llm import AssistantMessage, Model, ModelClientConfig, ModelRequestConfig, ToolCall
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig, CheckpointerFactory
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails.context_engineer.context_processor_rail import ContextProcessorRail

ORIGINAL_LOG = "full uncompressed log entry\n" * 1800


def build_agent(root: Path):
    """Use the existing factory/config/rail entry points without business wrappers."""
    model = Model(
        ModelClientConfig(client_provider="OpenAI", api_key="offline-example", api_base="http://localhost:1"),
        ModelRequestConfig(model="offline-example", max_tokens=128),
    )
    tool = LocalFunction(
        ToolCard(
            id="read_sample_log",
            name="read_sample_log",
            description="Read the sample log",
            input_params={"type": "object", "properties": {}},
        ),
        lambda: ORIGINAL_LOG,
    )
    return create_deep_agent(
        model,
        card=AgentCard(id="persistent-example"),
        system_prompt="Use tools to inspect logs.",
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


def scripted_provider(phase: str, label: str, other: str):
    """Substitute only the external model, while checking its actual input."""
    calls = 0

    async def invoke(messages, **kwargs):
        nonlocal calls
        calls += 1
        text = "\n".join(str(message.content) for message in messages)
        assert other not in text, "another Session leaked into the model input"
        if phase == "seed" and calls <= 2:
            if calls == 2:
                assert "OFFLOAD" in text, "large tool output was not archived before the next model call"
            return AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"{label}-{calls}",
                        type="function",
                        name="read_sample_log",
                        arguments="{}",
                    )
                ],
            )
        if phase == "resume":
            assert f"remember-{label}" in text
            assert "OFFLOAD" in text
            assert ORIGINAL_LOG not in text, "restoration must not rehydrate archived tool results"
        return AssistantMessage(content=f"remember-{label}" if phase == "seed" else f"restored-{label}")

    return invoke


def verify_files(root: Path, expected_executions: int) -> None:
    """Check original logs, stable provenance and Session separation on disk."""
    trajectories = list(root.glob("sessions/*/history/trajectories/*.jsonl"))
    assert len(trajectories) == expected_executions
    session_dirs = {path.parents[2] for path in trajectories}
    assert len(session_dirs) == 2
    for path in trajectories:
        records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        assert len({record["session_id"] for record in records}) == 1
        assert len({record["execution_id"] for record in records}) == 1
        assert all(record["occurred_at"] and record["archived_at"] for record in records)
    offloads = list(root.glob("sessions/*/history/offload/*.jsonl"))
    assert offloads
    assert any(
        ORIGINAL_LOG == json.loads(line)["message"]["content"]
        for path in offloads
        for line in path.read_text("utf-8").splitlines()
    )


async def run(root: Path, phase: str) -> None:
    """Restore once, invoke normally, save once; a fresh process can repeat this."""
    await anyio.Path(root).mkdir(parents=True, exist_ok=True)
    database = create_async_engine(f"sqlite+aiosqlite:///{(root / 'state.db').as_posix()}")
    previous = CheckpointerFactory.get_checkpointer()
    checkpointer = await CheckpointerFactory.create(
        CheckpointerConfig(
            type="persistence",
            conf={"db_type": "sqlite", "db_client": database},
        )
    )
    CheckpointerFactory.set_default_checkpointer(checkpointer)
    await Runner.start()
    try:
        agent = build_agent(root)
        for label, other in [("ALPHA", "BETA"), ("BETA", "ALPHA")]:
            session = create_agent_session(session_id=f"session-{label}", card=agent.card)
            # Host load: the native checkpoint restores the trimmed active state.
            await session.pre_run()
            if phase == "resume":
                assert session.get_state("context"), "seed this directory in a separate process first"
            query = f"Remember {label} and inspect two logs" if phase == "seed" else "What do you remember?"
            with patch.object(Model, "invoke", side_effect=scripted_provider(phase, label, other)):
                result = await agent.invoke({"query": query}, session)
            # DeepAgent has saved trajectory + active state. Host commits its Session.
            await session.post_run()
            expected = f"remember-{label}" if phase == "seed" else f"restored-{label}"
            assert result["output"] == expected
            print(f"{session.get_session_id()}: {result['output']}")
        verify_files(root, 2 if phase == "seed" else 4)
        print(f"ACCEPTANCE {phase}: PASS")
    finally:
        await Runner.stop()
        CheckpointerFactory.set_default_checkpointer(previous)
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--phase", choices=("seed", "resume"), required=True)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.root.resolve(), arguments.phase))
