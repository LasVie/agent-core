# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Capture originals before processors; never reconstruct a trace from a window."""

import uuid
from datetime import datetime, timezone
from typing import Any

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import BaseError, build_error
from openjiuwen.core.context_engine.context.context_utils import ContextUtils
from openjiuwen.core.context_engine.history.store import SessionHistoryStore
from openjiuwen.core.context_engine.schema.history import ArchiveRecord, ArchiveRef, ExecutionRecord
from openjiuwen.core.foundation.llm import BaseMessage


class SessionHistoryRecorder:
    """Session-owned execution state. JSON strings isolate records from mutations."""

    def __init__(self, store: SessionHistoryStore):
        self.store = store
        self.execution_id: str | None = None
        self.failure: BaseError | None = None
        self.references: dict[str, ArchiveRef] = {}
        self.last_execution: ExecutionRecord | None = None
        self._records: dict[str, str] = {}
        self._seq = 0
        self._step_id = ""
        self._tool_steps: dict[str, str] = {}

    def begin(self, execution_id: str | None = None) -> str:
        """Bind exactly one outer execution; reject concurrent Session writers."""
        if self.execution_id is not None:
            raise build_error(
                StatusCode.CONTEXT_EXECUTION_ERROR, error_msg="Session history execution is already active"
            )
        self.execution_id = execution_id or uuid.uuid4().hex
        self.failure = None
        self._seq = 0
        self._step_id = f"{self.execution_id}:input"
        self._tool_steps = {}
        return self.execution_id

    def capture(self, messages: list[BaseMessage], *, legacy: bool = False) -> None:
        """Freeze incoming messages before any processor can rewrite them."""
        ContextUtils.ensure_context_message_ids(messages)
        if not legacy and self.execution_id is None:
            raise build_error(
                StatusCode.CONTEXT_EXECUTION_ERROR, error_msg="begin Session history before adding messages"
            )
        for message in messages:
            message_id = message.metadata["context_message_id"]
            if message_id in self._records:
                continue
            if message.role == "assistant":
                self._step_id = message_id
                for call in getattr(message, "tool_calls", None) or []:
                    self._tool_steps[call.id] = message_id
            step_id = self._tool_steps.get(getattr(message, "tool_call_id", ""), self._step_id)
            record = ArchiveRecord(
                session_id=self.store.session_id,
                execution_id="legacy" if legacy else self.execution_id,
                step_id=step_id or message_id,
                seq=self._seq,
                message_id=message_id,
                occurred_at=None if legacy else datetime.now(timezone.utc),
                message=message.model_dump(mode="json"),
            )
            self._records[message_id] = record.model_dump_json()
            if not legacy:
                self._seq += 1

    def originals(self, messages: list[BaseMessage]) -> list[ArchiveRecord]:
        """Return independent original records, preserving legacy unknown times."""
        # Legacy checkpoints and archive-reference messages have no new occurrence time.
        self.capture(messages, legacy=True)
        return [ArchiveRecord.model_validate_json(self._records[m.metadata["context_message_id"]]) for m in messages]

    def current_records(self) -> list[ArchiveRecord]:
        """Export the current execution in arrival order."""
        records = [ArchiveRecord.model_validate_json(value) for value in self._records.values()]
        return sorted((r for r in records if r.execution_id == self.execution_id), key=lambda record: record.seq)

    def protected_user_ids(self) -> set[str]:
        """Identify all current-execution user instructions, including steering."""
        return {record.message_id for record in self.current_records() if record.message.get("role") == "user"}

    def finish(self, status: str = "completed") -> ExecutionRecord | None:
        """Publish the complete trajectory before closing the execution."""
        if self.execution_id is None:
            return None
        trajectory = self.store.write_trajectory(self.execution_id, self.current_records())
        result = ExecutionRecord(execution_id=self.execution_id, status=status, trajectory=trajectory)
        self.last_execution = result
        self.execution_id = None
        return result

    def snapshot(self, messages: list[BaseMessage]) -> dict[str, Any]:
        """Save provenance alongside the native active-message checkpoint."""
        records = self.originals(messages)
        # An in-progress checkpoint also retains already offloaded originals for
        # interrupted execution export. Completed snapshots contain only active originals.
        records_by_id = {record.message_id: record for record in records + self.current_records()}
        return {
            "records": [record.model_dump(mode="json") for record in records_by_id.values()],
            "references": [ref.model_dump(mode="json") for ref in self.references.values()],
            "last_execution": self.last_execution.model_dump(mode="json") if self.last_execution else None,
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Merge provenance without recording old messages as new input."""
        for value in state.get("records", []):
            record = ArchiveRecord.model_validate(value)
            if record.session_id != self.store.session_id:
                raise build_error(StatusCode.CONTEXT_ARCHIVE_EXECUTION_ERROR, error_msg="checkpoint Session mismatch")
            self._records.setdefault(record.message_id, record.model_dump_json())
        for value in state.get("references", []):
            ref = ArchiveRef.model_validate(value)
            self.references[ref.archive_id] = ref
        if state.get("last_execution"):
            self.last_execution = ExecutionRecord.model_validate(state["last_execution"])

    def retain(self, messages: list[BaseMessage]) -> None:
        """Release exported originals from RAM; referenced files remain on disk."""
        ids = {message.metadata.get("context_message_id") for message in messages}
        self._records = {key: value for key, value in self._records.items() if key in ids}
