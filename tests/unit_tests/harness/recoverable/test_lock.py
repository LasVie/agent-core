# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One session with rollback per workspace, and rounds that must not start."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest

from openjiuwen.core.common.exception.errors import BaseError, build_error
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.harness.recoverable import RecoverableHarness, RollbackApproval
from tests.unit_tests.fixtures.deep_agent_interrupts import CountingTool, run, scripted
from tests.unit_tests.fixtures.mock_llm import create_text_response
from tests.unit_tests.harness.recoverable.test_harness import _agent, _model, runner  # noqa: F401

pytestmark = pytest.mark.skipif(os.name != "posix", reason="flock")

_HOLDER = """
import sys
from pathlib import Path
from openjiuwen.harness.recoverable.lock import WorkspaceLock
lock = WorkspaceLock(Path(sys.argv[1]))
print("HOLDER:", lock.claim("other") or "held", flush=True)
sys.stdin.read()
"""


def _setup(tmp_path):
    harness = RecoverableHarness(kv_store=InMemoryKVStore(), storage_dir=tmp_path / "recoverable")
    return harness, _agent(tmp_path, harness, CountingTool("work"))


def _lock_file(tmp_path):
    """Where the spec puts the lock: ``locks/`` plus the workspace key, 16 hex digits of the root's sha256."""
    key = hashlib.sha256(str((tmp_path / "ws").resolve()).encode()).hexdigest()[:16]
    return tmp_path / "recoverable" / "locks" / f"{key}.lock"


@pytest.mark.asyncio
async def test_a_second_session_in_the_workspace_is_refused_until_the_first_is_released(tmp_path, runner):
    harness, agent = _setup(tmp_path)
    with scripted(_model(*[create_text_response(f"answer {i}") for i in range(4)])):
        await run(agent, "first", "s1")
        with pytest.raises(BaseError, match="session s1 already has rollback on"):
            await run(agent, "second", "s2")
        await run(agent, "first again", "s1")

        assert harness.release_session("s2") is False, "s2 never held the workspace"
        assert harness.release_session("s1") is True
        await run(agent, "second", "s2")

    c0 = (await harness.history("s1"))[-1]
    plan = await harness.plan_rollback("s1", c0.checkpoint_id)
    with pytest.raises(BaseError, match="session s2 already has rollback on"):
        await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=c0.checkpoint_id))
    assert len(await harness.history("s1")) > 1, "the refused rollback changed nothing"


@pytest.mark.asyncio
async def test_a_workspace_held_by_another_process_is_refused_until_that_process_ends(tmp_path, runner):
    harness, agent = _setup(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(_lock_file(tmp_path))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=os.getcwd(),
    )
    try:
        lines = iter(holder.stdout.readline, "")  # Imports log to stdout first.
        assert next((line.strip() for line in lines if line.startswith("HOLDER:")), "") == "HOLDER: held"
        with scripted(_model(create_text_response("never"), create_text_response("now"))):
            with pytest.raises(BaseError, match=f"another process .*pid {holder.pid}"):
                await run(agent, "work", "s1")
            holder.stdin.close()  # The other process ends; the system drops its lock.
            holder.wait(timeout=10)
            assert (await run(agent, "work", "s1"))["output"] == "never"
    finally:
        holder.kill()


@pytest.mark.asyncio
async def test_a_round_whose_rollback_cannot_be_written_back_does_not_run(tmp_path, runner, monkeypatch):
    harness, agent = _setup(tmp_path)
    llm = _model(create_text_response("first"), create_text_response("after rollback"))
    with scripted(llm):
        await run(agent, "first", "s1")
        c0 = (await harness.history("s1"))[-1]
        plan = await harness.plan_rollback("s1", c0.checkpoint_id)
        await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=c0.checkpoint_id))
        calls = len(llm.call_history)

        async def broken(*args):
            raise build_error(StatusCode.DEEPAGENT_RUNTIME_ERROR, error_msg="storage is unreachable")

        with monkeypatch.context() as patch:
            patch.setattr(harness, "_apply_pending", broken)
            with pytest.raises(BaseError, match="storage is unreachable"):
                await run(agent, "go on", "s1")
        assert len(llm.call_history) == calls, "the model never saw the context that was not rolled back"

        assert (await run(agent, "go on", "s1"))["output"] == "after rollback"
