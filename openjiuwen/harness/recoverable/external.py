# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Back up the files outside the workspace that a shell or code call declares, and put them back.

The model lists in ``external_paths`` the outside paths a call writes. Before the
call each path is backed up: file content goes to the object store and the list
of entries to the call's ledger record; a path that does not exist is recorded as
absent. Right after the call the path is recorded again, by content only.

A rollback puts a path back as it was before the call, or deletes it when it was
absent. If the path no longer holds what the call left, someone changed it since:
it is left alone and reported. A path that is too large or unreadable is not backed
up and cannot be put back. Everything here is synchronous; callers run it in a
worker thread.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from openjiuwen.harness.recoverable.manifest import Entry, EntryKind, Manifest
from openjiuwen.harness.recoverable.models import EffectOutcome, PathBackup
from openjiuwen.harness.recoverable.objects import ObjectStore, hash_file
from openjiuwen.harness.recoverable.workspace import WorkspaceSnapshot

MAX_ENTRIES = 10_000
"""Most entries backed up under one declared path; a larger directory is not backed up."""

Entries = dict[str, Entry]


def resolve_paths(declared: list[str], cwd: Path) -> list[Path]:
    """Declared paths made absolute, without duplicates. A symlink adds the path it points to.

    Directories above the last component are resolved, so the path compares with
    the resolved workspace root; the last component is kept, so a symlink is backed
    up as a link.
    """
    paths: list[Path] = []
    for raw in declared:
        text = raw.strip()
        if not text:
            continue
        path = Path(os.path.expanduser(text))
        path = Path(os.path.abspath(path if path.is_absolute() else cwd / path))
        path = Path(os.path.realpath(path.parent)) / path.name if path.name else path
        found = [path]
        if path.is_symlink():
            found.append(Path(os.path.realpath(path)))  # Writing through the link changes its target.
        paths.extend(p for p in found if p not in paths)
    return paths


def capture(
    path: Path, objects: ObjectStore | None, size_cap: int | None, max_entries: int | None = MAX_ENTRIES,
) -> tuple[Entries | None, str | None]:
    """What ``path`` holds, keyed relative to its parent; ``None`` when it does not exist.

    With ``objects`` file content is stored, otherwise only hashed. The second value
    says why the path could not be read whole: too many entries, more than
    ``size_cap`` bytes, or an entry that cannot be read.
    """
    entries: Entries = {}
    total = 0
    pending = [path]
    while pending:
        full = pending.pop()
        try:
            st = full.lstat()
            if stat.S_ISLNK(st.st_mode):
                entry = Entry(EntryKind.LINK, mode=st.st_mode, link=os.readlink(full))
            elif stat.S_ISDIR(st.st_mode):
                entry = Entry(EntryKind.DIR, mode=st.st_mode)
                pending.extend(full.iterdir())
            elif stat.S_ISREG(st.st_mode):
                total += st.st_size
                if size_cap is not None and total > size_cap:
                    return None, f"larger than {size_cap} bytes"
                digest = objects.put(full, st.st_size, capped=False) if objects is not None else hash_file(full)
                entry = Entry(EntryKind.FILE, digest, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode)
            else:
                continue  # Devices, pipes and sockets are not backed up.
        except FileNotFoundError:
            if full == path:
                return None, None
            continue  # Removed while being read.
        except OSError as exc:
            return None, f"cannot back up {full}: {exc.strerror or exc}"
        entries[full.relative_to(path.parent).as_posix()] = entry
        if max_entries is not None and len(entries) > max_entries:
            return None, f"more than {max_entries} entries"
    return entries, None


def _lists(entries: Entries | None) -> dict[str, list] | None:
    return {rel: entry.to_list() for rel, entry in entries.items()} if entries is not None else None


def _entries(lists: dict[str, list] | None) -> Entries | None:
    return {rel: Entry.from_list(raw) for rel, raw in lists.items()} if lists is not None else None


def _same(left: Entries | None, right: Entries | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return left.keys() == right.keys() and all(entry.same_content(right[rel]) for rel, entry in left.items())


def back_up_paths(
    declared: list[str],
    cwd: Path,
    workspace_root: Path,
    storage_dir: Path,
    objects: ObjectStore,
    size_cap: int | None,
) -> list[PathBackup]:
    """Back up each declared path outside the workspace before the call writes it."""
    backups = []
    for path in resolve_paths(declared, cwd):
        if path == workspace_root or workspace_root in path.parents:
            continue  # The workspace snapshot covers it.
        if path in workspace_root.parents:
            backups.append(PathBackup(path=str(path), error="it holds the workspace"))
        elif path == storage_dir or storage_dir in path.parents or path in storage_dir.parents:
            backups.append(PathBackup(path=str(path), error="it overlaps the rollback storage"))
        else:
            entries, error = capture(path, objects, size_cap)
            backups.append(PathBackup(path=str(path), before=_lists(entries), error=error))
    return backups


def record_after(backup: PathBackup, size_cap: int | None) -> PathBackup:
    """Note what the call left at a backed-up path, so a later change by someone else shows."""
    if backup.error is not None:
        return backup
    entries, error = capture(Path(backup.path), None, size_cap)
    if error is not None:
        return backup
    return backup.model_copy(update={"after": _lists(entries), "after_taken": True})


def put_back(backup: PathBackup, objects: ObjectStore, *, check_after: bool) -> tuple[EffectOutcome, str]:
    """Make ``backup.path`` hold what it held before the call; return the outcome and why.

    With ``check_after``, a path that no longer holds what the call left is kept.
    """
    path = Path(backup.path)
    before = _entries(backup.before)
    current, error = capture(path, None, None, max_entries=None)
    if error is not None:
        return EffectOutcome.FAILED, error
    if _same(current, before):
        return EffectOutcome.UNDONE, ""
    if check_after and not _same(current, _entries(backup.after)):
        return EffectOutcome.KEPT, "it changed again after the agent's call, so it was left as it is"
    report = WorkspaceSnapshot(path.parent, objects).restore(Manifest(entries=current or {}),
                                                              Manifest(entries=before or {}))
    for rel, entry in (before or {}).items():
        if entry.kind == EntryKind.DIR:  # restore() creates directories with default permissions.
            try:
                os.chmod(path.parent / rel, entry.mode & 0o7777)
            except OSError:
                report.failed.append(rel)
    restored, error = capture(path, None, None, max_entries=None)
    if error is None and _same(restored, before):
        return EffectOutcome.UNDONE, ""
    if report.unrecoverable:
        return EffectOutcome.FAILED, f"the backup of {', '.join(report.unrecoverable[:5])} is missing"
    failed = ", ".join(sorted(set(report.failed))[:5]) or error or str(path)
    return EffectOutcome.FAILED, f"could not write {failed}; it may need permissions this process lacks, such as sudo"
