# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Publication must succeed before any original may be removed."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.context_engine.history.store import SessionHistoryStore
from openjiuwen.core.context_engine.schema.history import ArchiveRecord, SessionHistoryConfig


def record(session_id="session-a"):
    return ArchiveRecord(
        session_id=session_id,
        execution_id="execution",
        step_id="step",
        seq=0,
        message_id="original-id",
        occurred_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        message={"role": "assistant", "content": "完整原文", "tool_calls": [{"arguments": "{}"}]},
    )


def test_lossless_and_idempotent(tmp_path):
    store = SessionHistoryStore(str(tmp_path), "session-a")
    original = record()
    ref = store.write_offload([original])
    before = Path(ref.path).read_bytes()
    assert store.write_offload([original]) == ref
    assert Path(ref.path).read_bytes() == before
    restored = ArchiveRecord.model_validate_json(before)
    assert restored.message == original.message
    assert restored.occurred_at == original.occurred_at
    assert restored.archived_at is not None
    assert store.write_trajectory("execution", [original]).path != ref.path


def test_sessions_isolated_and_identifiers_cannot_escape(tmp_path):
    first = SessionHistoryStore(str(tmp_path), "../../outside")
    second = SessionHistoryStore(str(tmp_path), "session-b")
    assert first.root != second.root
    assert first.root.is_relative_to(tmp_path)
    with pytest.raises(BaseError, match="ARCHIVE_FAILED"):
        first.write_offload([record()])
    assert not list(tmp_path.rglob("*.jsonl"))


def test_atomic_failure_and_conflicting_retry(tmp_path):
    store = SessionHistoryStore(str(tmp_path), "session-a")
    with patch("openjiuwen.core.context_engine.history.store.os.link", side_effect=OSError("disk full")):
        with pytest.raises(BaseError, match="disk full"):
            store.write_offload([record()])
    assert not list(tmp_path.rglob("*.jsonl"))
    assert not list(tmp_path.rglob("*.tmp"))
    ref = store.write_trajectory("execution", [record()])
    previous = Path(ref.path).read_bytes()
    with pytest.raises(BaseError, match="different originals"):
        store.write_trajectory("execution", [record().model_copy(update={"message": {"content": "changed"}})])
    assert Path(ref.path).read_bytes() == previous


def test_explicit_opt_in():
    assert not SessionHistoryConfig().enabled
    with pytest.raises(ValueError, match="root_dir"):
        SessionHistoryConfig(enabled=True)
