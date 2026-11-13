# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Workspace manifests: what every path held at one checkpoint.

A manifest maps a workspace-relative POSIX path to an :class:`Entry`. Files are
named by the sha256 of their content in the object store. The stat fields let the
next scan reuse a digest without reading the file again, the way the git index
does.

Stored manifests are deltas against the manifest before them, with a full one
every :data:`FULL_EVERY` records, so a step that changes one file stores one entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.harness.recoverable.models import SCHEMA_VERSION

FULL_EVERY = 50
"""Store a full manifest after this many deltas."""

RACY_WINDOW_NS = 2_000_000_000
"""A file modified this close to a scan may change again within the same mtime tick."""


class EntryKind(str, Enum):
    FILE = "f"
    LINK = "l"
    DIR = "d"
    SKIPPED = "s"
    """A regular file over the size cap: present, but its content is not stored."""


@dataclass(frozen=True)
class Entry:
    kind: EntryKind
    digest: str | None = None
    size: int = 0
    mtime_ns: int = 0
    ino: int = 0
    mode: int = 0
    link: str | None = None

    def same_content(self, other: "Entry | None") -> bool:
        """Same thing at this path, ignoring when it was last touched."""
        if other is None or self.kind != other.kind or (self.mode & 0o7777) != (other.mode & 0o7777):
            return False
        if self.kind == EntryKind.FILE:
            return self.digest == other.digest
        if self.kind == EntryKind.LINK:
            return self.link == other.link
        if self.kind == EntryKind.SKIPPED:
            return (self.size, self.mtime_ns, self.ino) == (other.size, other.mtime_ns, other.ino)
        return True

    def to_list(self) -> list:
        return [self.kind.value, self.digest, self.size, self.mtime_ns, self.ino, self.mode, self.link]

    @classmethod
    def from_list(cls, raw: list) -> "Entry":
        kind, digest, size, mtime_ns, ino, mode, link = raw
        return cls(EntryKind(kind), digest, size, mtime_ns, ino, mode, link)


@dataclass
class Manifest:
    entries: dict[str, Entry] = field(default_factory=dict)
    scan_ns: int = 0
    """When the scan that produced this manifest started."""
    build_outputs: list[str] = field(default_factory=list)
    """Build output directories seen by the scan; they are not in ``entries``."""

    def trusted(self, path: str, size: int, mtime_ns: int, ino: int, mode: int) -> Entry | None:
        """The cached entry for ``path`` when its stat still matches and is not racy."""
        entry = self.entries.get(path)
        if entry is None or entry.kind not in (EntryKind.FILE, EntryKind.SKIPPED):
            return None
        if (entry.size, entry.mtime_ns, entry.ino, entry.mode) != (size, mtime_ns, ino, mode):
            return None
        if entry.mtime_ns >= self.scan_ns - RACY_WINDOW_NS:
            return None
        return entry

    def same_content(self, other: "Manifest") -> bool:
        if self.entries.keys() != other.entries.keys():
            return False
        return all(entry.same_content(other.entries[path]) for path, entry in self.entries.items())

    def skipped(self) -> list[str]:
        return sorted(path for path, entry in self.entries.items() if entry.kind == EntryKind.SKIPPED)


@dataclass(frozen=True)
class ManifestRecord:
    """A stored manifest: full, or the changes since ``parent_id``."""

    manifest_id: str
    parent_id: str | None
    depth: int
    """Deltas since the last full manifest; 0 for a full one."""
    scan_ns: int
    entries: dict[str, list | None]
    """Path to encoded entry; ``None`` marks a deletion in a delta."""

    @property
    def full(self) -> bool:
        return self.parent_id is None

    @classmethod
    def build(cls, manifest_id: str, manifest: Manifest, base: "tuple[str, int, Manifest] | None") -> "ManifestRecord":
        """Encode ``manifest`` as a delta against ``base`` (id, depth, manifest), or in full."""
        if base is None or base[1] + 1 >= FULL_EVERY:
            entries = {path: entry.to_list() for path, entry in manifest.entries.items()}
            return cls(manifest_id, None, 0, manifest.scan_ns, entries)
        base_id, base_depth, base_manifest = base
        changes: dict[str, list | None] = {
            path: entry.to_list()
            for path, entry in manifest.entries.items()
            if not entry.same_content(base_manifest.entries.get(path))
        }
        changes.update({path: None for path in base_manifest.entries.keys() - manifest.entries.keys()})
        return cls(manifest_id, base_id, base_depth + 1, manifest.scan_ns, changes)

    def apply(self, base: Manifest | None) -> Manifest:
        entries = {} if self.full or base is None else dict(base.entries)
        for path, raw in self.entries.items():
            if raw is None:
                entries.pop(path, None)
            else:
                entries[path] = Entry.from_list(raw)
        return Manifest(entries=entries, scan_ns=self.scan_ns)

    def to_json(self) -> str:
        return json.dumps({
            "schema_version": SCHEMA_VERSION,
            "manifest_id": self.manifest_id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "scan_ns": self.scan_ns,
            "entries": self.entries,
        }, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "ManifestRecord":
        data = json.loads(raw)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise build_error(
                StatusCode.DEEPAGENT_RUNTIME_ERROR,
                error_msg=f"unknown manifest schema_version {data.get('schema_version')}",
            )
        return cls(data["manifest_id"], data["parent_id"], data["depth"], data["scan_ns"], data["entries"])
