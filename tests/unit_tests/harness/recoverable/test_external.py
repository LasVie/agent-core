# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Files outside the workspace that a shell call declares, with the real bash tool.

``outside/`` stands for the user's home: it is not in the workspace, so only the
backup taken before the call can put it back.
"""
from __future__ import annotations

import os
import stat

import pytest

from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.harness.recoverable import EffectAction, EffectOutcome, RecoverableHarness, RollbackStatus
from openjiuwen.harness.recoverable.external import resolve_paths
from tests.unit_tests.fixtures.deep_agent_interrupts import run, scripted
from tests.unit_tests.harness.recoverable.test_shell import _agent, _apply, _bash, _model, _plan, runner  # noqa: F401

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX shell and file modes")


def _setup(tmp_path, **options):
    harness = RecoverableHarness(kv_store=InMemoryKVStore(), storage_dir=tmp_path / "recoverable", **options)
    outside = tmp_path / "outside"
    outside.mkdir()
    return harness, _agent(tmp_path, harness), outside


def test_declared_paths_are_made_absolute_and_a_link_adds_its_target(tmp_path):
    (tmp_path / "real").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    home = os.path.expanduser("~")

    paths = resolve_paths(["link", "~/.zshrc", " ", "link"], tmp_path)

    root = tmp_path.resolve()
    assert [str(p) for p in paths] == [str(root / "link"), str(root / "real"), os.path.join(home, ".zshrc")]


@pytest.mark.asyncio
async def test_declared_outside_files_are_put_back_as_they_were(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path)
    rc, created, gone, config = outside / "rc", outside / "created", outside / "gone", outside / "config"
    rc.write_text("export A=1\n")
    gone.write_text("keep me\n")
    gone.chmod(0o600)
    (config / "sub").mkdir(parents=True)
    (config / "sub" / "a.toml").write_text("a = 1\n")
    command = (f"echo 'export B=2' >> {rc} && echo new > {created} && rm {gone} "
               f"&& rm -r {config}/sub && echo b > {config}/b.toml")
    with scripted(_model(_bash("c1", command, side_effect="none",
                               external_paths=[str(rc), str(created), str(gone), str(config)]))):
        await run(agent, "configure", "s1")

    plan = await _plan(harness)
    assert [(e.action, e.args_summary, e.detail) for e in plan.effects] == [
        (EffectAction.UNDO, str(rc), "put back as it was before the call"),
        (EffectAction.UNDO, str(created), "did not exist before the call; it is deleted"),
        (EffectAction.UNDO, str(gone), "put back as it was before the call"),
        (EffectAction.UNDO, str(config), "put back as it was before the call"),
    ]
    result = await _apply(harness, plan)

    assert result.status == RollbackStatus.COMPLETED
    assert [e.outcome for e in result.effects] == [EffectOutcome.UNDONE] * 4
    assert rc.read_text() == "export A=1\n"
    assert not created.exists()
    assert gone.read_text() == "keep me\n" and stat.S_IMODE(gone.stat().st_mode) == 0o600
    assert (config / "sub" / "a.toml").read_text() == "a = 1\n" and not (config / "b.toml").exists()


@pytest.mark.asyncio
async def test_a_file_changed_after_the_agent_is_kept_and_reported(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path)
    rc = outside / "rc"
    rc.write_text("export A=1\n")
    with scripted(_model(_bash("c1", f"echo 'export B=2' >> {rc}", side_effect="none", external_paths=[str(rc)]))):
        await run(agent, "configure", "s1")
    rc.write_text("the user's own edit\n")

    result = await _apply(harness, await _plan(harness))

    assert result.status == RollbackStatus.COMPLETED, "keeping the user's edit is not a failure"
    assert result.effects[0].outcome == EffectOutcome.KEPT
    assert rc.read_text() == "the user's own edit\n"


@pytest.mark.asyncio
async def test_two_calls_on_one_file_are_put_back_newest_first(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path)
    rc = outside / "rc"
    rc.write_text("original\n")
    with scripted(_model(
        _bash("c1", f"echo first > {rc}", side_effect="none", external_paths=[str(rc)]),
        _bash("c2", f"echo second > {rc}", side_effect="none", external_paths=[str(rc)]),
    )):
        await run(agent, "configure", "s1")

    result = await _apply(harness, await _plan(harness))

    assert [e.outcome for e in result.effects] == [EffectOutcome.UNDONE, EffectOutcome.UNDONE], (
        "putting c2 back recreates what c1 left, so c1's check still passes"
    )
    assert rc.read_text() == "original\n"


@pytest.mark.asyncio
async def test_paths_that_cannot_be_backed_up_are_irreversible(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path, file_size_cap=64)
    big = outside / "big"
    big.mkdir()
    (big / "data").write_text("x" * 100)
    inside = tmp_path / "ws" / "notes.txt"
    with scripted(_model(_bash("c1", f"echo more >> {big}/data && echo hi > {inside}", side_effect="none",
                               external_paths=[str(big), str(tmp_path), str(inside)]))):
        await run(agent, "write", "s1")

    plan = await _plan(harness)

    assert [(e.action, e.args_summary, e.detail) for e in plan.effects] == [
        (EffectAction.IRREVERSIBLE, str(big.resolve()), "not backed up: larger than 64 bytes"),
        (EffectAction.IRREVERSIBLE, str(tmp_path.resolve()), "not backed up: it holds the workspace"),
    ], "a path inside the workspace is left to the workspace snapshot"
    assert plan.files_to_delete == ["notes.txt"]


@pytest.mark.asyncio
async def test_when_what_the_call_left_is_unknown_the_person_decides(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path, file_size_cap=64)
    log = outside / "log"
    log.write_text("short\n")
    # The call leaves more than the cap, so what it left is not recorded.
    with scripted(_model(_bash("c1", f"printf '%0100d' 0 >> {log}", side_effect="none",
                               external_paths=[str(log)]))):
        await run(agent, "write", "s1")
    plan = await _plan(harness)
    assert plan.effects[0].action == EffectAction.REVIEW

    result = await _apply(harness, plan, confirmed_effect_ids=[plan.effects[0].record_id])
    assert result.effects[0].outcome == EffectOutcome.UNDONE
    assert log.read_text() == "short\n"


@pytest.mark.asyncio
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
async def test_a_path_that_cannot_be_written_back_fails_and_can_be_resumed(tmp_path, runner):
    harness, agent, outside = _setup(tmp_path)
    locked = outside / "locked"
    locked.mkdir()
    rc = locked / "rc"
    rc.write_text("original\n")
    with scripted(_model(_bash("c1", f"echo changed > {rc}", side_effect="none", external_paths=[str(rc)]))):
        await run(agent, "configure", "s1")
    locked.chmod(0o500)  # Like a file only sudo could write back.
    try:
        result = await _apply(harness, await _plan(harness))
    finally:
        locked.chmod(0o700)

    assert result.status == RollbackStatus.PARTIAL
    assert result.effects[0].outcome == EffectOutcome.FAILED and "permissions" in result.effects[0].detail
    assert rc.read_text() == "changed\n"

    resumed = await harness.resume_rollback(result.plan_id)
    assert resumed.status == RollbackStatus.COMPLETED
    assert rc.read_text() == "original\n"
