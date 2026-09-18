# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Strict, atomic, Session-scoped JSONL publication; no memory fallback."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import logger
from openjiuwen.core.context_engine.schema.history import ArchiveRecord, ArchiveRef


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(records: list[ArchiveRecord]) -> str:
    return json.dumps(
        [record.model_dump(mode="json", exclude={"archived_at"}) for record in records],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class SessionHistoryStore:
    """One writer per Session. Constructing the store does not create files."""

    def __init__(self, root_dir: str, session_id: str):
        self.session_id = session_id
        self.root = Path(root_dir).resolve() / "sessions" / _digest(session_id)

    def offload_path(self, records: list[ArchiveRecord]) -> Path:
        """Derive a stable content-addressed destination before writing."""
        return self.root / "history" / "offload" / f"{_digest(_canonical(records))}.jsonl"

    def write_offload(self, records: list[ArchiveRecord]) -> ArchiveRef:
        """Durably publish originals before the caller removes any messages."""
        return self._publish(self.offload_path(records), records)

    def write_trajectory(self, execution_id: str, records: list[ArchiveRecord]) -> ArchiveRef:
        """Publish one immutable full execution log."""
        path = self.root / "history" / "trajectories" / f"{_digest(execution_id)}.jsonl"
        return self._publish(path, records)

    def reference(self, path: Path, records: list[ArchiveRecord]) -> ArchiveRef:
        """Describe a file and its original message/time range."""
        times = [record.occurred_at for record in records if record.occurred_at is not None]
        return ArchiveRef(
            archive_id=path.stem,
            path=str(path),
            message_ids=tuple(record.message_id for record in records),
            first_occurred_at=min(times) if times else None,
            last_occurred_at=max(times) if times else None,
        )

    def _publish(self, path: Path, records: list[ArchiveRecord]) -> ArchiveRef:
        temporary = None
        try:
            if any(record.session_id != self.session_id for record in records):
                raise ValueError("records belong to another Session")
            # Check after resolution so a symlink inside history cannot redirect writes.
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("history path escapes the bound Session")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                previous = [ArchiveRecord.model_validate_json(line) for line in path.read_text("utf-8").splitlines()]
                if _canonical(previous) != _canonical(records):
                    raise ValueError("existing archive contains different originals")
                return self.reference(path, previous)
            archived_at = datetime.now(timezone.utc)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=".history-",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                for record in records:
                    output.write(record.model_copy(update={"archived_at": archived_at}).model_dump_json() + "\n")
                output.flush()
                os.fsync(output.fileno())
            # Atomic no-overwrite publication. Unlike replace(), this cannot destroy
            # another writer's file. A racing writer is an error (writers must serialize).
            os.link(temporary, path)
            return self.reference(path, records)
        except (OSError, ValueError, TypeError) as error:
            raise build_error(StatusCode.CONTEXT_ARCHIVE_EXECUTION_ERROR, error_msg=str(error), cause=error) from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Unable to remove history temporary file %s", temporary, exc_info=True)
