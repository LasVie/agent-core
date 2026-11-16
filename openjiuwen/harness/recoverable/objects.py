# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Content-addressed file store for workspace snapshots.

Each stored file is named by the sha256 of its content, so identical content is
stored once. On file systems that support clones (APFS, btrfs, XFS) a file is
cloned: both copies share blocks until one changes. Elsewhere it is copied, and
files over the size cap are not stored. Everything here is synchronous; callers
run it in a worker thread.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import shutil
import sys
import uuid
from pathlib import Path

_CHUNK = 1 << 20
_FICLONE = 0x40049409  # Linux ioctl: reflink the whole file.

_libc = None
if sys.platform == "darwin":
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def clone_file(src: Path, dst: Path) -> bool:
    """Clone ``src`` to a new ``dst``; False when the file system cannot."""
    try:
        if _libc is not None:
            return _libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0
        if sys.platform.startswith("linux"):
            import fcntl

            with open(src, "rb") as source, open(dst, "wb") as target:
                fcntl.ioctl(target.fileno(), _FICLONE, source.fileno())
            return True
    except OSError:
        dst.unlink(missing_ok=True)
    return False


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_and_hash(src: Path, dst: Path) -> str:
    digest = hashlib.sha256()
    with open(src, "rb") as source, open(dst, "wb") as target:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


class ObjectStore:
    """Stores and materializes file content under ``root``.

    Args:
        root: Directory of this workspace's objects. Must be on the workspace's
            volume for clones to work; otherwise files are copied.
        size_cap: Largest file stored when cloning is unavailable; ``None`` for no cap.
        use_clones: Try file clones before copying.
    """

    def __init__(self, root: Path, *, size_cap: int | None, use_clones: bool = True):
        self._root = root
        self._tmp = root / "tmp"
        self._size_cap = size_cap
        self._clones = use_clones
        self.added_bytes = 0
        """Size of the objects this instance stored, so the total can be kept without walking the store."""

    @property
    def root(self) -> Path:
        return self._root

    def _objects(self):
        """Every stored object as ``(digest, path, size)``."""
        if not self._root.is_dir():
            return
        for prefix in os.scandir(self._root):
            if prefix.name == self._tmp.name or not prefix.is_dir(follow_symlinks=False):
                continue
            for item in os.scandir(prefix.path):
                if item.is_file(follow_symlinks=False):
                    yield prefix.name + item.name, Path(item.path), item.stat(follow_symlinks=False).st_size

    def usage(self) -> int:
        """Total size of the stored objects. Clones may share blocks, so the disk may hold less."""
        return sum(size for _, _, size in self._objects())

    def unused_bytes(self, live: set[str]) -> int:
        """Size of the objects not in ``live``: what :meth:`sweep` would free."""
        return sum(size for digest, _, size in self._objects() if digest not in live)

    def sweep(self, live: set[str]) -> int:
        """Delete the objects not in ``live``; return the bytes freed.

        Only call it while nothing is being stored: an object stored but not yet
        referenced by a record would look unused.
        """
        freed = 0
        for digest, path, size in list(self._objects()):
            if digest not in live:
                path.unlink(missing_ok=True)
                freed += size
        return freed

    def path(self, digest: str) -> Path:
        return self._root / digest[:2] / digest[2:]

    def has(self, digest: str) -> bool:
        return self.path(digest).is_file()

    def put(self, src: Path, size: int, *, capped: bool = True) -> str | None:
        """Store ``src``; return its digest, or ``None`` when it is over the cap."""
        self._tmp.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp / uuid.uuid4().hex
        try:
            if self._clones and clone_file(src, tmp):
                digest = hash_file(tmp)  # Hash the clone: it cannot change underneath us.
            else:
                self._clones = False
                if capped and self._size_cap is not None and size > self._size_cap:
                    return None
                digest = _copy_and_hash(src, tmp)
            final = self.path(digest)
            if final.exists():
                tmp.unlink()
            else:
                final.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(tmp, 0o444)
                os.replace(tmp, final)
                self.added_bytes += size
            return digest
        finally:
            tmp.unlink(missing_ok=True)

    def materialize(self, digest: str, dst: Path, mode: int) -> None:
        """Atomically replace ``dst`` with the stored content and permission bits."""
        tmp = dst.parent / f".{dst.name}.recoverable-{uuid.uuid4().hex[:8]}"
        try:
            if not (self._clones and clone_file(self.path(digest), tmp)):
                shutil.copyfile(self.path(digest), tmp)
            os.chmod(tmp, mode & 0o7777)
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
