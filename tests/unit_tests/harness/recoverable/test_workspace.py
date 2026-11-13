# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Workspace snapshots and file rollback on a real DeepAgent.

A tool writes files the way a shell would, without telling anyone; the test also
edits files directly, as the user would. Rollback must put the workspace back to
the checkpoint either way.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.harness.recoverable import RecoverableHarness, RollbackApproval
from openjiuwen.harness.recoverable.workspace import WorkspaceSnapshot
from tests.unit_tests.fixtures.deep_agent_interrupts import make_deep_agent, messages_seen, run, scripted
from tests.unit_tests.fixtures.mock_llm import MockLLMModel, create_text_response, create_tool_call_response

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest_asyncio.fixture
async def runner():
    await Runner.start()
    try:
        yield
    finally:
        await Runner.stop()


class FileTool(Tool):
    """Writes, deletes or runs git in the workspace directly, like a shell command would."""

    def __init__(self, root: Path):
        super().__init__(ToolCard(
            id="edit",
            name="edit",
            description="change the workspace",
            input_params={"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "delete": {"type": "boolean"},
                "mtime_ns": {"type": "integer"},
                "git": {"type": "array", "items": {"type": "string"}},
            }},
        ))
        self.root = root

    async def invoke(self, inputs, **kwargs):
        if inputs.get("git"):
            _git(self.root, *inputs["git"])
            return {"ok": True}
        path = self.root / inputs["path"]
        if inputs.get("delete"):
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inputs["content"])
            if "mtime_ns" in inputs:
                os.utime(path, ns=(inputs["mtime_ns"], inputs["mtime_ns"]))
        return {"ok": True}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _call(call_id: str, **args) -> object:
    return create_tool_call_response("edit", json.dumps(args), tool_call_id=call_id)


def _model(*responses) -> MockLLMModel:
    llm = MockLLMModel()
    llm.set_responses(list(responses))
    return llm


def _setup(tmp_path, files: dict[str, str] | None = None, **harness_kwargs):
    ws = tmp_path / "ws"
    ws.mkdir()
    for name, content in (files or {}).items():
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text(content)
    kwargs = {"kv_store": InMemoryKVStore(), "storage_dir": tmp_path / "recoverable", **harness_kwargs}
    harness = RecoverableHarness(**kwargs)
    agent = make_deep_agent(ws, tools=[FileTool(ws)], rails=[harness.rail()], name="RecoverableFiles")
    return ws, harness, agent


async def _rollback(harness, session_id: str, seq: int):
    target = next(c for c in await harness.history(session_id) if c.seq == seq)
    plan = await harness.plan_rollback(session_id, target.checkpoint_id, reason="test")
    result = await harness.apply_rollback(RollbackApproval(plan_id=plan.plan_id, target_id=target.checkpoint_id))
    return plan, result


@pytest.mark.asyncio
async def test_rollback_restores_files_whoever_changed_them(tmp_path, runner):
    ws, harness, agent = _setup(tmp_path, {"notes.md": "orig notes", "report.md": "r0"})
    llm = _model(
        _call("c1", path="report.md", content="r1"),
        _call("c2", path="report.md", content="r2"),
        _call("c3", path="summary.md", content="summary"),
        create_text_response("done"),
        create_text_response("again"),
    )
    with scripted(llm):
        await run(agent, "write the report", "s1")
        (ws / "notes.md").write_text("user edit")  # The user edits a file outside the agent.

        plan, result = await _rollback(harness, "s1", 1)
        assert plan.workspace_restored
        assert plan.files_to_write == ["notes.md", "report.md"]
        assert plan.files_to_delete == ["summary.md"]
        assert (ws / "report.md").read_text() == "r1"
        assert (ws / "notes.md").read_text() == "orig notes", "the user's edit is rolled back too"
        assert not (ws / "summary.md").exists()
        assert result.files_written == ["notes.md", "report.md"] and result.files_deleted == ["summary.md"]

        await run(agent, "try again", "s1")

    contents = [str(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in messages_seen(llm)]
    note = next(text for text in contents if text.startswith("[ROLLBACK]"))
    assert "Workspace files are back" in note and "2 written back, 1 deleted" in note


@pytest.mark.asyncio
async def test_the_first_snapshot_is_taken_before_any_tool_runs(tmp_path, runner):
    ws, harness, agent = _setup(tmp_path, {"a.txt": "orig"})
    original_scan = WorkspaceSnapshot.scan
    calls = []

    def slow_scan(self, cache):
        calls.append(time.monotonic())
        if len(calls) == 1:
            time.sleep(0.3)  # The background snapshot is still running when the model asks for the tool.
        return original_scan(self, cache)

    with patch.object(WorkspaceSnapshot, "scan", slow_scan), scripted(
        _model(_call("c1", path="a.txt", content="changed"), create_text_response("done"))
    ):
        await run(agent, "change a", "s1")
        assert (ws / "a.txt").read_text() == "changed"
        await _rollback(harness, "s1", 0)

    assert (ws / "a.txt").read_text() == "orig"


@pytest.mark.asyncio
@needs_git
async def test_dependency_dirs_are_kept_and_build_outputs_deleted(tmp_path, runner):
    ws, harness, agent = _setup(tmp_path, {
        ".gitignore": "dist/\n",
        "dist/app.js": "old bundle",
        "build/src.c": "v1",
        "node_modules/pkg/index.js": "v1",
    })
    _git(ws, "init", "-q")
    with scripted(_model(
        _call("c1", path="build/src.c", content="v2"),
        _call("c2", path="dist/app.js", content="new bundle"),
        _call("c3", path="node_modules/pkg/index.js", content="v2"),
        create_text_response("done"),
    )):
        await run(agent, "build", "s1")
        plan, result = await _rollback(harness, "s1", 0)

    assert plan.build_outputs_to_delete == ["dist"] and result.build_outputs_deleted == ["dist"]
    assert not (ws / "dist").exists(), "an ignored build output is deleted"
    assert (ws / "build/src.c").read_text() == "v1", "build/ is not ignored, so it is snapshotted"
    assert (ws / "node_modules/pkg/index.js").read_text() == "v2", "dependency directories are not rolled back"
    assert "node_modules/pkg/index.js" not in plan.files_to_write


@pytest.mark.asyncio
@needs_git
async def test_git_commits_after_the_checkpoint_are_reported_and_undone(tmp_path, runner):
    ws, harness, agent = _setup(tmp_path, {"a.txt": "one"})
    _git(ws, "init", "-q")
    _git(ws, "add", "a.txt")
    _git(ws, "commit", "-q", "-m", "first")
    with scripted(_model(
        _call("c1", path="a.txt", content="two"),
        _call("c2", git=["commit", "-q", "-am", "second"]),
        create_text_response("done"),
    )):
        await run(agent, "commit", "s1")
        assert _git(ws, "rev-list", "--count", "HEAD") == "2"
        plan, _ = await _rollback(harness, "s1", 0)

    assert plan.lost_commits == 1
    assert _git(ws, "rev-list", "--count", "HEAD") == "1"
    assert (ws / "a.txt").read_text() == "one"
    assert _git(ws, "status", "--porcelain", "--untracked-files=no") == "", "index and work tree match HEAD"


@pytest.mark.asyncio
async def test_a_racy_same_size_rewrite_is_still_detected(tmp_path, runner):
    ws, harness, agent = _setup(tmp_path, {"a.txt": "aaaa"})
    tick = time.time_ns()
    with scripted(_model(
        _call("c1", path="a.txt", content="bbbb", mtime_ns=tick),
        # Same size, same inode and same mtime: only the content differs.
        _call("c2", path="a.txt", content="cccc", mtime_ns=tick),
        create_text_response("done"),
    )):
        await run(agent, "rewrite", "s1")
        await _rollback(harness, "s1", 1)

    assert (ws / "a.txt").read_text() == "bbbb"


@pytest.mark.asyncio
async def test_files_over_the_cap_are_listed_and_reported(tmp_path, runner):
    big = 2_000_000  # Larger than every file DeepAgent puts in its workspace.
    ws, harness, agent = _setup(
        tmp_path, {"big.bin": "x" * big, "small.txt": "s"}, file_size_cap=big // 2, use_clones=False,
    )
    with scripted(_model(
        _call("c1", path="big.bin", content="y" * big),
        _call("c2", path="small.txt", content="t"),
        create_text_response("done"),
    )):
        await run(agent, "change both", "s1")
        assert await harness.uncovered_files("s1") == ["big.bin"]
        plan, result = await _rollback(harness, "s1", 0)

    assert plan.unrecoverable_files == ["big.bin"] and result.unrestored_files == ["big.bin"]
    assert (ws / "small.txt").read_text() == "s"
    assert (ws / "big.bin").read_text() == "y" * big


@pytest.mark.asyncio
async def test_files_are_restored_after_a_restart(tmp_path, runner):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("v0")
    storage = tmp_path / "recoverable"
    first = RecoverableHarness(storage_dir=storage)
    with scripted(_model(
        _call("c1", path="a.txt", content="v1"),
        _call("c2", path="a.txt", content="v2"),
        create_text_response("done"),
    )):
        await run(make_deep_agent(ws, tools=[FileTool(ws)], rails=[first.rail()]), "edit", "s1")

    second = RecoverableHarness(storage_dir=storage)
    agent = make_deep_agent(ws, tools=[FileTool(ws)], rails=[second.rail()])
    await agent.ensure_initialized()  # Binds the workspace before any round runs.
    await _rollback(second, "s1", 1)

    assert (ws / "a.txt").read_text() == "v1"
