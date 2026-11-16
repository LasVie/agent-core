# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Freeing stored data: after a rollback, over the storage limits, and when a session is deleted.

Stored file content is checked on disk where the spec puts it:
``storage_dir/objects/<workspace key>/`` named by sha256.
"""
from __future__ import annotations

import hashlib
import shutil

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.harness.recoverable import EventKind
from openjiuwen.harness.recoverable.cleanup import backup_digests, manifest_digests
from openjiuwen.harness.recoverable.manifest import Entry, EntryKind, ManifestRecord
from openjiuwen.harness.recoverable.models import EffectCategory, LedgerRecord, PathBackup
from tests.unit_tests.fixtures.deep_agent_interrupts import run, scripted
from tests.unit_tests.fixtures.mock_llm import create_text_response
from tests.unit_tests.harness.recoverable.test_workspace import _call, _model, _rollback, _setup, runner  # noqa: F401

_KB = 1024


def _stored(tmp_path, content: str) -> bool:
    key = hashlib.sha256(str((tmp_path / "ws").resolve()).encode()).hexdigest()[:16]
    digest = hashlib.sha256(content.encode()).hexdigest()
    return (tmp_path / "recoverable" / "objects" / key / digest[:2] / digest[2:]).is_file()


def _versions(count: int, size: int) -> list[str]:
    return [str(i) * size for i in range(1, count + 1)]


def _writes(versions: list[str]) -> list:
    return [_call(f"c{i}", path="a.txt", content=v) for i, v in enumerate(versions, 1)] + [create_text_response("done")]


def test_only_digests_a_rollback_could_write_back_are_kept():
    def entry(digest):
        return Entry(EntryKind.FILE, digest, mode=0o644).to_list()

    full = ManifestRecord("m1", None, 0, 0, {"a": entry("old-a"), "b": entry("b")})
    delta = ManifestRecord("m2", "m1", 1, 0, {"a": entry("new-a")})
    assert manifest_digests({"m1": full, "m2": delta}, {"m2"}) == {"new-a", "b"}, (
        "the base record stays, but the content its delta replaced is not needed"
    )

    def record(seq, digest, **fields):
        backup = PathBackup(path="/x", before={"x": entry(digest)})
        return LedgerRecord(record_id=f"r{seq}", session_id="s", seq=seq, tool_call_id="", tool_name="bash",
                            category=EffectCategory.NONE, external_backups=[backup], **fields)

    records = [record(1, "before-floor"), record(3, "live"), record(4, "abandoned", abandoned=True),
               record(5, "retried", abandoned=True)]
    assert backup_digests(records, floor=2, retried={"r5"}) == {"live", "retried"}


@pytest.mark.asyncio
async def test_a_rollback_frees_the_content_only_the_discarded_steps_held(tmp_path, runner):
    _, harness, agent = _setup(tmp_path, files={"a.txt": "v0"})
    v1, v2 = "one" * _KB, "two" * _KB
    with scripted(_model(*_writes([v1, v2]))):
        await run(agent, "edit", "s1")
    assert all(_stored(tmp_path, v) for v in ("v0", v1, v2))

    await _rollback(harness, "s1", 1)

    assert _stored(tmp_path, "v0") and _stored(tmp_path, v1)
    assert not _stored(tmp_path, v2), "only the discarded step held it"


@pytest.mark.asyncio
async def test_over_the_cap_the_oldest_checkpoints_go_first_and_it_is_announced(tmp_path, runner):
    events = []
    versions = _versions(5, 200 * _KB)
    _, harness, agent = _setup(tmp_path, storage_cap=450 * _KB, on_event=events.append)
    with scripted(_model(*_writes(versions))):
        await run(agent, "edit", "s1")

    history = await harness.history("s1")
    # Round start, five step ends, round end. Dropping C0..C3 frees v1..v3 and brings it under 450 KB.
    assert [c.seq for c in reversed(history)] == [4, 5, 6]
    assert [_stored(tmp_path, v) for v in versions] == [False, False, False, True, True]
    dropped = [e for e in events if e.kind == EventKind.CHECKPOINTS_DROPPED]
    assert len(dropped) == 1 and dropped[0].detail.startswith("4 oldest checkpoint(s) deleted because stored")

    await _rollback(harness, "s1", 4)
    assert (tmp_path / "ws" / "a.txt").read_text() == versions[3], "the oldest remaining checkpoint still works"


@pytest.mark.asyncio
async def test_a_nearly_full_disk_keeps_only_the_latest_checkpoint(tmp_path, runner, monkeypatch):
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda path: real(path)._replace(free=real(path).total // 10))
    events = []
    _, harness, agent = _setup(tmp_path, on_event=events.append)
    with scripted(_model(*_writes(["x", "y"]))):
        await run(agent, "edit", "s1")

    [head] = await harness.history("s1")
    assert head.seq == 3
    assert "less than 20% of the disk is free" in next(
        e.detail for e in events if e.kind == EventKind.CHECKPOINTS_DROPPED
    )


@pytest.mark.asyncio
async def test_deleting_a_session_removes_its_records_history_and_content(tmp_path, runner):
    kv = InMemoryKVStore()
    _, harness, agent = _setup(tmp_path, kv_store=kv)
    with scripted(_model(*_writes(["s1 only"]))):
        await run(agent, "edit", "s1")
    target = (await harness.history("s1"))[-1]
    plan = await harness.plan_rollback("s1", target.checkpoint_id)

    harness.release_session("s1")
    await harness.delete_session("s1")

    assert await harness.history("s1") == []
    assert await kv.get_by_prefix("recoverable:s1:") == {} and await kv.get_by_prefix("s1:") == {}
    assert await kv.get(f"recoverable:plan:{plan.plan_id}") is None
    assert not _stored(tmp_path, "s1 only")
    with pytest.raises(BaseError):
        await harness.plan_rollback("s1", target.checkpoint_id)
