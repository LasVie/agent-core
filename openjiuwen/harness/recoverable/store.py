# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Checkpoint, plan and pending-rollback records over a ``BaseKVStore``.

Keys live under ``recoverable:``. vcs keeps its own keys under
``{session_id}:vcs:`` in the same store, so the two never collide.
"""

from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.store.base_kv_store import BaseKVStore
from openjiuwen.harness.recoverable.manifest import ManifestRecord
from openjiuwen.harness.recoverable.models import (
    SCHEMA_VERSION,
    Checkpoint,
    LedgerRecord,
    PendingRollback,
    RollbackPlan,
)

_PREFIX = "recoverable"
_RecordT = TypeVar("_RecordT", bound=BaseModel)


def _load(model: type[_RecordT], raw: str | bytes | None) -> _RecordT | None:
    if raw is None:
        return None
    record = model.model_validate_json(raw)
    version = getattr(record, "schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise build_error(
            StatusCode.DEEPAGENT_RUNTIME_ERROR,
            error_msg=f"unknown {model.__name__} schema_version {version}",
        )
    return record


class CheckpointStore:
    """Persists checkpoints, the HEAD pointer, rollback plans and pending rollbacks."""

    def __init__(self, kv_store: BaseKVStore):
        self._kv = kv_store

    @staticmethod
    def _checkpoint_key(session_id: str, checkpoint_id: str) -> str:
        return f"{_PREFIX}:{session_id}:checkpoint:{checkpoint_id}"

    @staticmethod
    def _head_key(session_id: str) -> str:
        return f"{_PREFIX}:{session_id}:head"

    @staticmethod
    def _pending_key(session_id: str) -> str:
        return f"{_PREFIX}:{session_id}:pending"

    @staticmethod
    def _plan_key(plan_id: str) -> str:
        return f"{_PREFIX}:plan:{plan_id}"

    @staticmethod
    def _manifest_key(session_id: str, manifest_id: str) -> str:
        return f"{_PREFIX}:{session_id}:manifest:{manifest_id}"

    @staticmethod
    def _workspace_scan_key(workspace_key: str) -> str:
        return f"{_PREFIX}:workspace:{workspace_key}:scan"

    @staticmethod
    def _ledger_prefix(session_id: str) -> str:
        return f"{_PREFIX}:{session_id}:ledger:"

    @staticmethod
    def _ledger_seq_key(session_id: str) -> str:
        return f"{_PREFIX}:{session_id}:ledger_seq"

    async def get(self, session_id: str, checkpoint_id: str) -> Checkpoint | None:
        return _load(Checkpoint, await self._kv.get(self._checkpoint_key(session_id, checkpoint_id)))

    async def put(self, checkpoint: Checkpoint) -> None:
        key = self._checkpoint_key(checkpoint.session_id, checkpoint.checkpoint_id)
        await self._kv.set(key, checkpoint.model_dump_json())

    async def head(self, session_id: str) -> Checkpoint | None:
        head_id = await self._kv.get(self._head_key(session_id))
        if head_id is None:
            return None
        if isinstance(head_id, bytes):
            head_id = head_id.decode()
        return await self.get(session_id, head_id)

    async def set_head(self, session_id: str, checkpoint_id: str) -> None:
        await self._kv.set(self._head_key(session_id), checkpoint_id)

    async def chain(self, session_id: str, limit: int | None = None) -> list[Checkpoint]:
        """Checkpoints from HEAD back along ``parent_id``, newest first."""
        result: list[Checkpoint] = []
        current = await self.head(session_id)
        while current is not None and (limit is None or len(result) < limit):
            result.append(current)
            if current.parent_id is None:
                break
            current = await self.get(session_id, current.parent_id)
        return result

    async def delete(self, session_id: str, checkpoint_ids: list[str]) -> None:
        if checkpoint_ids:
            await self._kv.batch_delete([self._checkpoint_key(session_id, cid) for cid in checkpoint_ids])

    async def get_pending(self, session_id: str) -> PendingRollback | None:
        return _load(PendingRollback, await self._kv.get(self._pending_key(session_id)))

    async def put_pending(self, pending: PendingRollback) -> None:
        await self._kv.set(self._pending_key(pending.session_id), pending.model_dump_json())

    async def clear_pending(self, session_id: str) -> None:
        await self._kv.delete(self._pending_key(session_id))

    async def get_plan(self, plan_id: str) -> RollbackPlan | None:
        return _load(RollbackPlan, await self._kv.get(self._plan_key(plan_id)))

    async def put_plan(self, plan: RollbackPlan) -> None:
        await self._kv.set(self._plan_key(plan.plan_id), plan.model_dump_json())

    async def get_manifest(self, session_id: str, manifest_id: str) -> ManifestRecord | None:
        raw = await self._kv.get(self._manifest_key(session_id, manifest_id))
        return ManifestRecord.from_json(raw) if raw is not None else None

    async def put_manifest(self, session_id: str, record: ManifestRecord) -> None:
        await self._kv.set(self._manifest_key(session_id, record.manifest_id), record.to_json())

    async def delete_manifests(self, session_id: str, manifest_ids: list[str]) -> None:
        if manifest_ids:
            await self._kv.batch_delete([self._manifest_key(session_id, mid) for mid in manifest_ids])

    async def get_workspace_scan(self, workspace_key: str) -> ManifestRecord | None:
        """The latest full scan of a workspace, shared by its sessions to reuse digests."""
        raw = await self._kv.get(self._workspace_scan_key(workspace_key))
        return ManifestRecord.from_json(raw) if raw is not None else None

    async def put_workspace_scan(self, workspace_key: str, record: ManifestRecord) -> None:
        await self._kv.set(self._workspace_scan_key(workspace_key), record.to_json())

    async def get_packages(self, session_id: str, packages_id: str) -> dict[str, dict[str, str]] | None:
        """Package lists by workspace-relative environment path."""
        raw = await self._kv.get(f"{_PREFIX}:{session_id}:packages:{packages_id}")
        return json.loads(raw) if raw is not None else None

    async def put_packages(self, session_id: str, packages_id: str, packages: dict[str, dict[str, str]]) -> None:
        await self._kv.set(f"{_PREFIX}:{session_id}:packages:{packages_id}",
                           json.dumps(packages, sort_keys=True, separators=(",", ":")))

    async def ledger_seq(self, session_id: str) -> int:
        """Highest ``seq`` handed out in the session's side-effect ledger; 0 when empty."""
        raw = await self._kv.get(self._ledger_seq_key(session_id))
        return int(raw) if raw is not None else 0

    async def next_ledger_seq(self, session_id: str) -> int:
        """Reserve the next ``seq``. Callers serialize this per session."""
        seq = await self.ledger_seq(session_id) + 1
        await self._kv.set(self._ledger_seq_key(session_id), str(seq))
        return seq

    async def put_record(self, record: LedgerRecord) -> None:
        await self._kv.set(f"{self._ledger_prefix(record.session_id)}{record.seq:012d}", record.model_dump_json())

    async def records(self, session_id: str) -> list[LedgerRecord]:
        """The session's side-effect records in registration order."""
        raw = await self._kv.get_by_prefix(self._ledger_prefix(session_id))
        loaded = [_load(LedgerRecord, value) for _, value in sorted(raw.items())]
        return [record for record in loaded if record is not None]
