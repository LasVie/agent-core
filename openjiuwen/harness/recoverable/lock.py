# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One session with rollback per workspace at a time.

A rollback returns the whole workspace to a checkpoint and cannot be undone, so
a second session working in the same workspace would lose its changes to the
first one's rollback. The first session to start a round holds the workspace
until it is released or its process ends.

Inside a process the holder is a session id; across processes it is an
exclusive ``flock`` on a file under the storage directory, which the operating
system drops when the process exits, even after a crash. Without ``fcntl``
(Windows) only the in-process check applies.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None


class WorkspaceLock:
    """Holds one workspace for one session. Synchronous and quick; safe to call from async code."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None
        self.holder: str | None = None
        """The session holding the workspace in this process, if any."""

    def claim(self, session_id: str) -> str | None:
        """Hold the workspace for ``session_id``; return why not when another session holds it."""
        if self.holder == session_id:
            return None
        if self.holder is not None:
            return f"session {self.holder} already has rollback on in this workspace"
        if fcntl is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holder = os.read(fd, 200).decode(errors="replace").strip() or "unknown"
                os.close(fd)
                return f"another process already has rollback on in this workspace ({holder})"
            os.ftruncate(fd, 0)
            os.write(fd, f"session {session_id}, pid {os.getpid()}".encode())
            self._fd = fd
        self.holder = session_id
        return None

    def release(self, session_id: str | None = None) -> bool:
        """Let the workspace go; with ``session_id``, only when that session holds it."""
        if self.holder is None or (session_id is not None and session_id != self.holder):
            return False
        if self._fd is not None:
            os.close(self._fd)  # Closing the descriptor drops the flock.
            self._fd = None
        self.holder = None
        return True
