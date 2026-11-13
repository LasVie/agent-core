# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scan a workspace into a manifest, and make the workspace match a manifest again.

Two kinds of directory stay out of the snapshot:

- dependency directories, named in ``dependency_dirs`` at any depth;
- build outputs, named in ``build_output_dirs`` **and** ignored by git, so a
  ``build/`` that holds sources is still snapshotted. Rollback deletes them.

``.git`` is snapshotted like any other directory and is exempt from the size cap.
Everything here is synchronous; callers run it in a worker thread.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from openjiuwen.core.common.logging import logger
from openjiuwen.harness.recoverable.manifest import Entry, EntryKind, Manifest
from openjiuwen.harness.recoverable.objects import ObjectStore

DEFAULT_DEPENDENCY_DIRS = frozenset({"node_modules", ".venv", "venv", ".tox"})
DEFAULT_BUILD_OUTPUT_DIRS = frozenset({"dist", "build", "target", ".next"})
_GIT_TIMEOUT_S = 10


def _git(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=stdin, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class WorkspaceDiff:
    """What making the workspace match a target manifest involves."""

    write: list[str] = field(default_factory=list)
    """Files and links to write back."""
    delete: list[str] = field(default_factory=list)
    """Files and links that did not exist at the target."""
    unrecoverable: list[str] = field(default_factory=list)
    """Paths whose target content was never stored (over the cap or unreadable)."""
    build_outputs: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.write or self.delete or self.unrecoverable or self.build_outputs)


@dataclass
class RestoreReport:
    written: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    build_outputs_deleted: list[str] = field(default_factory=list)
    unrecoverable: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _depth(path: str) -> int:
    return path.count("/")


class WorkspaceSnapshot:
    """Scans and restores one workspace directory against an :class:`ObjectStore`."""

    def __init__(
        self,
        root: Path,
        objects: ObjectStore,
        *,
        dependency_dirs: frozenset[str] = DEFAULT_DEPENDENCY_DIRS,
        build_output_dirs: frozenset[str] = DEFAULT_BUILD_OUTPUT_DIRS,
    ):
        self.root = root
        self.objects = objects
        self._dependency_dirs = dependency_dirs
        self._build_output_dirs = build_output_dirs

    # --------------------------------------------------------------------- scan

    def scan(self, cache: Manifest | None) -> Manifest:
        """Record every path under the root; reuse digests whose stat ``cache`` still trusts."""
        manifest = Manifest(scan_ns=time.time_ns())
        if not self.root.is_dir():
            return manifest
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            base = Path(dirpath)
            candidates = [
                (base / name).relative_to(self.root).as_posix()
                for name in dirnames
                if name in self._build_output_dirs and not (base / name).is_symlink()
            ]
            ignored = self._git_ignored(candidates) if candidates else set()
            manifest.build_outputs.extend(sorted(ignored))
            kept = []
            for name in dirnames:
                full = base / name
                rel = full.relative_to(self.root).as_posix()
                if full.is_symlink():
                    manifest.entries[rel] = self._link_entry(full)
                elif name in self._dependency_dirs or rel in ignored:
                    continue
                else:
                    manifest.entries[rel] = Entry(EntryKind.DIR, mode=full.lstat().st_mode)
                    kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                full = base / name
                rel = full.relative_to(self.root).as_posix()
                entry = self._file_entry(full, rel, cache)
                if entry is not None:
                    manifest.entries[rel] = entry
        return manifest

    def _link_entry(self, full: Path) -> Entry:
        st = full.lstat()
        return Entry(EntryKind.LINK, size=st.st_size, mtime_ns=st.st_mtime_ns, ino=st.st_ino,
                     mode=st.st_mode, link=os.readlink(full))

    def _file_entry(self, full: Path, rel: str, cache: Manifest | None) -> Entry | None:
        try:
            st = full.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return self._link_entry(full)
        if not stat.S_ISREG(st.st_mode):
            return None  # Devices, pipes and sockets are not snapshotted.
        if cache is not None:
            trusted = cache.trusted(rel, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode)
            if trusted is not None and (trusted.digest is None or self.objects.has(trusted.digest)):
                return trusted
        capped = not (rel == ".git" or rel.startswith(".git/"))
        try:
            digest = self.objects.put(full, st.st_size, capped=capped)
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("recoverable: cannot read %s; it is not snapshotted", rel)
            digest = None
        kind = EntryKind.FILE if digest is not None else EntryKind.SKIPPED
        return Entry(kind, digest, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode)

    def _git_ignored(self, paths: list[str]) -> set[str]:
        """The ``paths`` git ignores; none when this is not a git work tree."""
        completed = _git(self.root, "check-ignore", "--stdin", stdin="\n".join(p + "/" for p in paths))
        if completed is None or completed.returncode not in (0, 1):
            return set()
        return {line.rstrip("/") for line in completed.stdout.splitlines() if line}

    # ------------------------------------------------------------------ restore

    def diff(self, current: Manifest, target: Manifest) -> WorkspaceDiff:
        result = WorkspaceDiff(build_outputs=list(current.build_outputs))
        for path, entry in target.entries.items():
            if entry.kind == EntryKind.DIR:
                continue
            existing = current.entries.get(path)
            if entry.kind == EntryKind.SKIPPED or (entry.kind == EntryKind.FILE and not self.objects.has(entry.digest)):
                if not entry.same_content(existing):
                    result.unrecoverable.append(path)
            elif not entry.same_content(existing):
                result.write.append(path)
        for path, entry in current.entries.items():
            if entry.kind != EntryKind.DIR and path not in target.entries:
                result.delete.append(path)
        for paths in (result.write, result.delete, result.unrecoverable):
            paths.sort()
        return result

    def restore(self, current: Manifest, target: Manifest) -> RestoreReport:
        """Make the workspace hold exactly what ``target`` recorded."""
        plan = self.diff(current, target)
        report = RestoreReport(unrecoverable=list(plan.unrecoverable))
        for directory in plan.build_outputs:
            self._remove(directory, report.build_outputs_deleted, report.failed)
        # A path that changes kind (file <-> directory) is cleared before it is written.
        for path, entry in current.entries.items():
            wanted = target.entries.get(path)
            if entry.kind == EntryKind.DIR and wanted is not None and wanted.kind != EntryKind.DIR:
                self._remove(path, [], report.failed)
        for path in sorted(plan.delete, key=_depth, reverse=True):
            self._remove(path, report.deleted, report.failed)
        for path in sorted((p for p, e in target.entries.items() if e.kind == EntryKind.DIR), key=_depth):
            full = self.root / path
            try:
                if full.is_symlink() or (full.exists() and not full.is_dir()):
                    full.unlink()
                full.mkdir(parents=True, exist_ok=True)
            except OSError:
                report.failed.append(path)
        for path in plan.write:
            if self._write(path, target.entries[path]):
                report.written.append(path)
            else:
                report.failed.append(path)
        extra_dirs = [p for p, e in current.entries.items() if e.kind == EntryKind.DIR and p not in target.entries]
        for path in sorted(extra_dirs, key=_depth, reverse=True):
            try:
                (self.root / path).rmdir()  # Only when empty: it may still hold dependencies.
            except OSError:
                pass
        return report

    def _write(self, path: str, entry: Entry) -> bool:
        full = self.root / path
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            if full.is_dir() and not full.is_symlink():
                shutil.rmtree(full)
            if entry.kind == EntryKind.LINK:
                if full.is_symlink() or full.exists():
                    full.unlink()
                os.symlink(entry.link, full)
            else:
                self.objects.materialize(entry.digest, full, entry.mode)
            return True
        except OSError:
            logger.warning("recoverable: failed to restore %s", path, exc_info=True)
            return False

    def _remove(self, path: str, done: list[str], failed: list[str]) -> None:
        full = self.root / path
        try:
            if full.is_dir() and not full.is_symlink():
                shutil.rmtree(full)
            else:
                full.unlink(missing_ok=True)
            done.append(path)
        except OSError:
            logger.warning("recoverable: failed to remove %s", path, exc_info=True)
            failed.append(path)

    # ---------------------------------------------------------------------- git

    def lost_commits(self, target: Manifest) -> int | None:
        """Commits on the current branches that the target's branch tips do not reach.

        ``None`` when the workspace has no ``.git`` or git is unavailable.
        """
        tips = self._branch_tips(target)
        if not (self.root / ".git").is_dir():
            return None
        args = ["rev-list", "--count", "--branches"]
        if tips:
            args += ["--not", *sorted(tips)]
        completed = _git(self.root, *args)
        if completed is None or completed.returncode != 0:
            return None
        try:
            return int(completed.stdout.strip())
        except ValueError:
            return None

    def _branch_tips(self, target: Manifest) -> set[str]:
        tips: set[str] = set()
        for path, entry in target.entries.items():
            if entry.kind != EntryKind.FILE or not entry.digest or not self.objects.has(entry.digest):
                continue
            if path.startswith(".git/refs/heads/") or path == ".git/packed-refs":
                content = self.objects.path(entry.digest).read_text(errors="replace")
                for line in content.splitlines():
                    sha = line.split(" ", 1)[0].strip()
                    if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
                        tips.add(sha)
        return tips
