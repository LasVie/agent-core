# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""What stored data a workspace still needs, so the rest can be deleted.

File content is shared by every session of a workspace and named by its digest.
A digest is still needed while some rollback could write it back:

- it is in the workspace as some remaining checkpoint (or a pending rollback, or
  the latest scan) recorded it. Manifests are deltas, so each such manifest is
  rebuilt from its chain; a digest that a later delta replaced is not needed
  even though the older record that names it is kept as a base;
- it is the backup of an outside file whose call a rollback could still undo:
  the record comes after the oldest remaining checkpoint and is not abandoned,
  or a partial rollback may still retry it.

Everything here is pure and synchronous.
"""

from __future__ import annotations

from openjiuwen.harness.recoverable.manifest import ManifestRecord
from openjiuwen.harness.recoverable.models import LedgerRecord

_DIGEST = 1
"""Position of the digest in an encoded manifest entry."""


def _chain(records: dict[str, ManifestRecord], root: str) -> list[ManifestRecord] | None:
    """The records from ``root`` back to its full manifest; ``None`` when one is missing."""
    chain: list[ManifestRecord] = []
    current: str | None = root
    while current is not None:
        record = records.get(current)
        if record is None:
            return None
        chain.append(record)
        current = record.parent_id
    return chain


def reachable_manifests(records: dict[str, ManifestRecord], roots: set[str]) -> set[str]:
    """Manifest ids needed to rebuild every root: the roots and their bases."""
    needed: set[str] = set()
    for root in roots:
        needed.update(record.manifest_id for record in _chain(records, root) or [])
    return needed


def manifest_digests(records: dict[str, ManifestRecord], roots: set[str]) -> set[str]:
    """Digests of the files each root manifest holds once rebuilt from its chain."""
    digests: set[str] = set()
    for root in roots:
        chain = _chain(records, root)
        if chain is None:
            continue
        manifest = None
        for record in reversed(chain):
            manifest = record.apply(manifest)
        digests.update(entry.digest for entry in manifest.entries.values() if entry.digest)
    return digests


def backup_digests(records: list[LedgerRecord], floor: int, retried: set[str]) -> set[str]:
    """Digests of outside-file backups a rollback could still put back.

    ``floor`` is the ledger position of the oldest remaining checkpoint: calls at
    or before it can no longer be rolled back. ``retried`` are the records a
    partial rollback may still retry, abandoned or not.
    """
    digests: set[str] = set()
    for record in records:
        if record.record_id not in retried and (record.abandoned or record.seq <= floor):
            continue
        for backup in record.external_backups:
            digests.update(raw[_DIGEST] for raw in (backup.before or {}).values() if raw[_DIGEST])
    return digests
