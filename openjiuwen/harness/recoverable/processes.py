# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Stop the background processes an agent started, and only those.

A background shell runs in its own session (``start_new_session``), so its pid
is also the id of its process group. The start time recorded at launch tells a
live process apart from an unrelated one that reuses the pid, for example after
a restart. Everything here is synchronous; callers run it in a worker thread.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

_PS_TIMEOUT_S = 5
_GRACE_S = 2.0


def process_start_time(pid: int) -> str | None:
    """The start time ``ps`` reports for ``pid``; ``None`` when it is not running or unknown."""
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = completed.stdout.strip()
    return started if completed.returncode == 0 and started else None


def _alive(pid: int) -> bool:
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)  # Our own child: reap it so it does not linger as a zombie.
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_group(pid: int, started: str | None) -> str:
    """Terminate the process group of ``pid`` if it is still the one recorded. Return what happened."""
    if not hasattr(os, "killpg"):
        return "not stopped: this platform cannot signal a process group"
    current = process_start_time(pid)
    if current is None:
        return "already exited"
    if started is not None and current != started:
        return "not stopped: the pid now belongs to another process"
    try:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + _GRACE_S
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _alive(pid):
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "already exited"
    except OSError as exc:
        return f"not stopped: {exc}"
    return "stopped"
