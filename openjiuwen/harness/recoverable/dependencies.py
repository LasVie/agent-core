# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Package lists of dependency environments, and the commands that restore them.

Dependency directories stay out of the workspace snapshot. Instead each checkpoint
records which packages every environment in the workspace held, and a rollback
installs and uninstalls until each environment holds that list again, whoever
changed it.

Two kinds of environment are read straight from disk, without running a package
manager:

- a Python virtual environment (a directory with ``pyvenv.cfg``): the
  ``*.dist-info`` entries of its ``site-packages``;
- a Node ``node_modules`` next to a ``package.json``: the ``package.json`` of each
  top-level package. Only npm projects are restored; pnpm and yarn are reported.

Global installs (``npm -g``, ``brew``, ``pip`` outside a virtual environment) are
compared before and after the command that made them, and only that difference is
reversed. Everything here is synchronous; callers run it in a worker thread.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from openjiuwen.harness.recoverable.effects import simple_commands, split_program

_RUN_TIMEOUT_S = 600
_LIST_TIMEOUT_S = 60

Packages = dict[str, str]
"""Package name to version."""


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _run(argv: list[str], cwd: Path | None = None, timeout: int = _RUN_TIMEOUT_S) -> tuple[bool, str]:
    try:
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output[-500:]


# ---------------------------------------------------------------- environments


def env_kind(path: Path) -> str | None:
    """``"python"``, ``"node"`` or ``None`` for a dependency directory."""
    if (path / "pyvenv.cfg").is_file():
        return "python"
    if path.name == "node_modules" and (path.parent / "package.json").is_file():
        return "node"
    return None


def _site_packages(path: Path) -> list[Path]:
    return sorted(p for p in path.glob("lib/python*/site-packages") if p.is_dir()) + \
        sorted(p for p in path.glob("Lib/site-packages") if p.is_dir())


def fingerprint(path: Path) -> str:
    """Cheap change marker: modification times of the directories installs touch."""
    kind = env_kind(path)
    if kind == "python":
        marks = [p for p in _site_packages(path)]
    elif kind == "node":
        marks = [path, path / ".package-lock.json"]
    else:
        return ""
    return ",".join(f"{p.stat().st_mtime_ns}" if p.exists() else "-" for p in marks)


def list_packages(path: Path) -> Packages:
    """What the environment at ``path`` holds, read from disk."""
    kind = env_kind(path)
    packages: Packages = {}
    if kind == "python":
        for dist in metadata.distributions(path=[str(p) for p in _site_packages(path)]):
            name = dist.metadata.get("Name")
            if name:
                packages[_normalize(name)] = dist.version
    elif kind == "node":
        manifests = list(path.glob("*/package.json")) + list(path.glob("@*/*/package.json"))
        for manifest in manifests:
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("name") and data.get("version"):
                packages[data["name"]] = str(data["version"])
    return packages


@dataclass
class PackageChange:
    """What restoring one environment involves."""

    env: str
    """Workspace-relative path of the environment."""
    remove: list[str] = field(default_factory=list)
    install: dict[str, str] = field(default_factory=dict)
    """Package to the version to install."""
    note: str = ""
    """Why it cannot be restored, when it cannot."""


def diff_packages(env: str, current: Packages, target: Packages) -> PackageChange:
    return PackageChange(
        env=env,
        remove=sorted(set(current) - set(target)),
        install={name: version for name, version in sorted(target.items()) if current.get(name) != version},
    )


def _python_steps(path: Path, change: PackageChange) -> list[list[str]] | None:
    """pip commands for a virtual environment: its own pip, else ``uv pip`` aimed at it."""
    python = path / "bin" / "python"
    if not python.exists():
        python = path / "Scripts" / "python.exe"
    if not python.exists():
        return None
    installs = [f"{n}=={v}" for n, v in change.install.items()]
    ok, _ = _run([str(python), "-m", "pip", "--version"], timeout=_LIST_TIMEOUT_S)
    if ok:
        pip = [str(python), "-m", "pip"]
        remove, install = [*pip, "uninstall", "-y"], [*pip, "install"]
    elif uv := shutil.which("uv"):
        remove = [uv, "pip", "uninstall", "--python", str(python)]
        install = [uv, "pip", "install", "--python", str(python)]
    else:
        return None
    return ([[*remove, *change.remove]] if change.remove else []) + ([[*install, *installs]] if installs else [])


def restore_environment(root: Path, change: PackageChange) -> str | None:
    """Make one environment hold its target list again; return an error, or ``None``."""
    path = root / change.env
    kind = env_kind(path)
    if kind == "python":
        steps = _python_steps(path, change)
        if steps is None:
            return "neither pip in the environment nor uv is available"
    elif kind == "node":
        project = path.parent
        if not (project / "package-lock.json").exists() and (
            (project / "pnpm-lock.yaml").exists() or (project / "yarn.lock").exists()
        ):
            return "only npm projects are restored; pnpm and yarn are not"
        npm = shutil.which("npm")
        if npm is None:
            return "npm is not available"
        steps = []
        if change.remove:
            steps.append([npm, "uninstall", "--no-save", *change.remove])
        if change.install:
            steps.append([npm, "install", "--no-save", *(f"{n}@{v}" for n, v in change.install.items())])
        path = project
    else:
        return "not a recognized environment"
    for argv in steps:
        ok, output = _run(argv, cwd=path)
        if not ok:
            return f"`{shlex.join(argv)}` failed: {output}"
    return None


# --------------------------------------------------------------------- global


def global_manager(command: str) -> str | None:
    """The global package manager a shell command installs with: ``npm``, ``brew`` or ``pip:<program>``."""
    for argv in simple_commands(command):
        program, args = split_program(argv)
        if program == "npm" and args[:1] and args[0] in ("i", "install", "add", "uninstall", "remove", "rm") \
                and ("-g" in args or "--global" in args):
            return "npm"
        if program == "brew" and args[:1] and args[0] in ("install", "uninstall", "upgrade", "reinstall"):
            return "brew"
        pip_args = args[2:] if program.startswith("python") and args[:2] == ["-m", "pip"] else (
            args if program in ("pip", "pip3") else None)
        if pip_args is not None and pip_args[:1] and pip_args[0] in ("install", "uninstall") \
                and not os.environ.get("VIRTUAL_ENV"):
            return f"pip:{program}" if program in ("pip", "pip3") else f"pip:{program} -m pip"
    return None


def list_global(manager: str) -> Packages | None:
    """Globally installed packages of ``manager``; ``None`` when it cannot be listed."""
    if manager == "npm":
        ok, output = _run(["npm", "ls", "-g", "--depth=0", "--json"], timeout=_LIST_TIMEOUT_S)
        try:
            deps = json.loads(output).get("dependencies", {}) if output else {}
        except ValueError:
            return None
        return {name: str(info.get("version", "")) for name, info in deps.items()} if ok or deps else None
    if manager == "brew":
        ok, output = _run(["brew", "list", "--versions"], timeout=_LIST_TIMEOUT_S)
        if not ok:
            return None
        return {line.split()[0]: line.split()[-1] for line in output.splitlines() if len(line.split()) >= 2}
    if manager.startswith("pip:"):
        program = shlex.split(manager[4:])
        ok, output = _run([*program, "list", "--format=json"], timeout=_LIST_TIMEOUT_S)
        if not ok:
            return None
        try:
            return {_normalize(p["name"]): p["version"] for p in json.loads(output)}
        except (ValueError, KeyError, TypeError):
            return None
    return None


def global_undo(manager: str, before: Packages, after: Packages) -> tuple[str | None, list[str]]:
    """A shell command reversing what changed between ``before`` and ``after``, and what it cannot reverse."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(n for n in set(before) & set(after) if before[n] != after[n])
    steps: list[str] = []
    cannot: list[str] = []
    if manager == "npm":
        if added:
            steps.append(shlex.join(["npm", "uninstall", "-g", *added]))
        if removed or changed:
            steps.append(shlex.join(["npm", "install", "-g", *(f"{n}@{before[n]}" for n in removed + changed)]))
    elif manager == "brew":
        if added:
            steps.append(shlex.join(["brew", "uninstall", *added]))
        if removed:
            steps.append(shlex.join(["brew", "install", *removed]))
        cannot = [f"{n} {before[n]} -> {after[n]}" for n in changed]  # brew cannot reinstall an old version
    elif manager.startswith("pip:"):
        program = shlex.split(manager[4:])
        if added:
            steps.append(shlex.join([*program, "uninstall", "-y", *added]))
        if removed or changed:
            steps.append(shlex.join([*program, "install", *(f"{n}=={before[n]}" for n in removed + changed)]))
    return (" && ".join(steps) or None), cannot
