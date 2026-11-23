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
  ``*.dist-info`` entries of its ``site-packages``. A package installed from a
  local path, an archive URL or a git commit keeps that source (PEP 610
  ``direct_url.json``) and is reinstalled from it: asking an index for the same
  name could fetch an unrelated package;
- a Node ``node_modules`` next to a ``package.json``: the ``package.json`` of each
  top-level package; a package that did not come from a registry keeps its source
  from npm's hidden lockfile (``node_modules/.package-lock.json``). The caller
  first puts back the target's ``package.json`` and lockfile. An npm project is
  then restored by list with ``--no-save`` (npm would otherwise re-add what a
  changed ``package.json`` names); a pnpm or yarn project is installed from its
  lockfile and compared with the target's list.

Global installs (``npm -g``, ``pnpm -g``, ``yarn global``, ``brew``, ``pip``
outside a virtual environment) are compared before and after the command that
made them, and only that difference is reversed. Everything here is synchronous;
callers run it in a worker thread.
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
"""Package name to version. A package installed from a direct source has ``"<version> @ <source>"``,
where the source is a pip requirement URL, or ``editable <URL>`` for an editable install."""
_SOURCE = " @ "
_EDITABLE = "editable "


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _run(argv: list[str], cwd: Path | None = None, timeout: int = _RUN_TIMEOUT_S) -> tuple[bool, str]:
    """Run a command; return whether it succeeded and the tail of its output, for error messages."""
    ok, stdout, stderr = _capture(argv, cwd, timeout)
    return ok, (stdout + stderr).strip()[-500:]


def _capture(argv: list[str], cwd: Path | None = None, timeout: int = _RUN_TIMEOUT_S) -> tuple[bool, str, str]:
    """Run a command; return whether it succeeded, its standard output and its error output apart.

    Listings parse the standard output only: package managers print notices,
    such as a newer version being available, on the error output.
    """
    try:  # No input: a package manager that would ask a question fails instead of waiting.
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
                                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", str(exc)
    return completed.returncode == 0, completed.stdout, completed.stderr


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


def _direct_source(dist: metadata.Distribution) -> str | None:
    """Where a package installed from a path, a URL or a VCS came from (PEP 610); ``None`` for an index install."""
    try:
        data = json.loads(dist.read_text("direct_url.json") or "null")
    except ValueError:
        return None
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str):
        return None
    vcs = data.get("vcs_info")
    if isinstance(vcs, dict) and vcs.get("vcs") and vcs.get("commit_id"):
        return f"{vcs['vcs']}+{url}@{vcs['commit_id']}"
    dir_info = data.get("dir_info")
    if isinstance(dir_info, dict) and dir_info.get("editable"):
        return _EDITABLE + url
    return url


def _requirement(name: str, version: str) -> list[str]:
    """pip arguments that install ``name`` at ``version``, from its direct source when it has one."""
    if _SOURCE not in version:
        return [f"{name}=={version}"]
    source = version.split(_SOURCE, 1)[1]
    if source.startswith(_EDITABLE):
        return ["-e", source[len(_EDITABLE):]]
    if re.match(r"^[a-z]+\+", source):  # A VCS URL; pip rejects "name @ git+file:///..." but takes it bare.
        return [source]
    return [f"{name} @ {source}"]


def _npm_sources(path: Path) -> dict[str, str]:
    """Top-level packages of ``node_modules`` that did not come from a registry, with their source.

    A local directory is a link resolved relative to the project; git and other
    URLs are kept as npm recorded them. A registry tarball (``.../-/<name>.tgz``)
    has no source to keep.
    """
    try:
        entries = json.loads((path / ".package-lock.json").read_text(encoding="utf-8")).get("packages", {})
    except (OSError, ValueError, AttributeError):
        return {}
    sources = {}
    for key, entry in entries.items() if isinstance(entries, dict) else ():
        name = key[len("node_modules/"):] if key.startswith("node_modules/") else ""
        resolved = entry.get("resolved") if isinstance(entry, dict) else None
        if not name or "/node_modules/" in name or not isinstance(resolved, str):
            continue
        if entry.get("link") or not ("/-/" in resolved and resolved.endswith(".tgz")):
            sources[name] = resolved
    return sources


def _npm_spec(name: str, version: str) -> str:
    """What ``npm install`` takes to install ``name`` at ``version``, from its source when it has one."""
    return version.split(_SOURCE, 1)[1] if _SOURCE in version else f"{name}@{version}"


def list_packages(path: Path) -> Packages:
    """What the environment at ``path`` holds, read from disk."""
    kind = env_kind(path)
    packages: Packages = {}
    if kind == "python":
        for dist in metadata.distributions(path=[str(p) for p in _site_packages(path)]):
            name = dist.metadata.get("Name")
            if name:
                source = _direct_source(dist)
                packages[_normalize(name)] = dist.version + (_SOURCE + source if source else "")
    elif kind == "node":
        sources = _npm_sources(path)
        manifests = list(path.glob("*/package.json")) + list(path.glob("@*/*/package.json"))
        for manifest in manifests:
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("name") and data.get("version"):
                name = manifest.parent.relative_to(path).as_posix()  # The directory npm installed it as.
                source = sources.get(name)
                packages[name] = str(data["version"]) + (_SOURCE + source if source else "")
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
    installs = [arg for n, v in change.install.items() for arg in _requirement(n, v)]
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


def node_manager(project: Path) -> str:
    """The package manager of a Node project, by its lockfile: ``npm``, ``pnpm`` or ``yarn``."""
    if not (project / "package-lock.json").exists():
        if (project / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (project / "yarn.lock").exists():
            return "yarn"
    return "npm"


def _lockfile_install(program: str, manager: str, project: Path) -> list[str]:
    """Install exactly what the project's lockfile names, without changing the lockfile."""
    if manager == "pnpm":
        return [program, "install", "--frozen-lockfile", "--prefer-offline", "--config.confirmModulesPurge=false"]
    if (project / ".yarnrc.yml").exists():  # Yarn 2 and later.
        return [program, "install", "--immutable"]
    return [program, "install", "--frozen-lockfile", "--prefer-offline", "--non-interactive"]


def restore_problem(path: Path) -> str | None:
    """Why the environment at ``path`` cannot be restored, known before a rollback is confirmed; ``None`` if it can."""
    kind = env_kind(path)
    if kind == "python":
        return None if _python_steps(path, PackageChange(env="")) is not None else \
            "neither pip in the environment nor uv is available"
    if kind == "node":
        manager = node_manager(path.parent)
        return None if shutil.which(manager) else f"{manager} is not available"
    return "not a recognized environment"


def restore_environment(root: Path, change: PackageChange, target: Packages | None = None) -> str | None:
    """Make one environment hold its target list again; return an error, or ``None``.

    An npm project and a virtual environment are changed by the list. A pnpm or
    yarn project is installed from its lockfile, which the caller has put back to
    the target's; ``target`` is then compared with what the install left.
    """
    path = root / change.env
    problem = restore_problem(path)
    if problem is not None:
        return problem
    lockfile = False
    if env_kind(path) == "python":
        steps = _python_steps(path, change) or []
    else:
        project = path.parent
        manager = node_manager(project)
        program = shutil.which(manager)
        steps = []
        if manager != "npm":
            lockfile = True
            steps.append(_lockfile_install(program, manager, project))
        else:
            if change.remove:
                steps.append([program, "uninstall", "--no-save", "--no-fund", "--no-audit", *change.remove])
            if change.install:
                steps.append([program, "install", "--no-save", "--no-fund", "--no-audit",
                              *(_npm_spec(n, v) for n, v in change.install.items())])
        path = project
    for argv in steps:
        ok, output = _run(argv, cwd=path)
        if not ok:
            return f"`{shlex.join(argv)}` failed: {output}"
    if lockfile and target is not None:
        left = diff_packages(change.env, list_packages(root / change.env), target)
        if left.remove or left.install:
            parts = [f"remove {n}" for n in left.remove] + [f"install {n}=={v}" for n, v in left.install.items()]
            return f"`{shlex.join(steps[0])}` left it different from the target: {', '.join(parts)}"
    return None


# --------------------------------------------------------------------- global


_PNPM_GLOBAL_VERBS = ("add", "i", "install", "remove", "rm", "uninstall", "un", "update", "up", "upgrade")


def global_manager(command: str) -> str | None:
    """The global package manager a shell command installs with: ``npm``, ``pnpm``, ``yarn``, ``brew`` or
    ``pip:<program>``."""
    for argv in simple_commands(command):
        program, args = split_program(argv)
        if program == "npm" and args[:1] and args[0] in ("i", "install", "add", "uninstall", "remove", "rm") \
                and ("-g" in args or "--global" in args):
            return "npm"
        if program == "pnpm" and args[:1] and args[0] in _PNPM_GLOBAL_VERBS and ("-g" in args or "--global" in args):
            return "pnpm"
        if program == "yarn" and args[:1] == ["global"] and args[1:2] and args[1] in ("add", "remove", "upgrade"):
            return "yarn"
        if program == "brew" and args[:1] and args[0] in ("install", "uninstall", "upgrade", "reinstall"):
            return "brew"
        pip_args = args[2:] if program.startswith("python") and args[:2] == ["-m", "pip"] else (
            args if program in ("pip", "pip3") else None)
        if pip_args is not None and pip_args[:1] and pip_args[0] in ("install", "uninstall") \
                and not os.environ.get("VIRTUAL_ENV"):
            return f"pip:{program}" if program in ("pip", "pip3") else f"pip:{program} -m pip"
    return None


def _yarn_global() -> Packages | None:
    """Yarn's global packages, read from its global folder.

    ``yarn global list`` leaves out packages without executables, so the folder's
    ``package.json`` names the packages and its ``node_modules`` gives their versions.
    """
    ok, output, _ = _capture(["yarn", "global", "dir"], timeout=_LIST_TIMEOUT_S)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not ok or not lines:
        return None
    folder = Path(lines[-1])
    try:
        names = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("dependencies") or {}
    except FileNotFoundError:
        return {}  # Nothing was ever installed globally.
    except (OSError, ValueError, AttributeError):
        return None
    installed = list_packages(folder / "node_modules")
    return {name: installed[name] for name in names if name in installed}


def list_global(manager: str) -> Packages | None:
    """Globally installed packages of ``manager``; ``None`` when it cannot be listed."""
    if manager == "npm":
        ok, output, _ = _capture(["npm", "ls", "-g", "--depth=0", "--json"], timeout=_LIST_TIMEOUT_S)
        try:
            deps = json.loads(output).get("dependencies", {}) if output else {}
        except ValueError:
            return None
        return {name: str(info.get("version", "")) for name, info in deps.items()} if ok or deps else None
    if manager == "pnpm":
        ok, output, _ = _capture(["pnpm", "ls", "-g", "--depth=0", "--json"], timeout=_LIST_TIMEOUT_S)
        try:
            entries = json.loads(output) if ok and output else None
        except ValueError:
            return None
        if not isinstance(entries, list):
            return None
        return {name: str(info.get("version", "")) for entry in entries if isinstance(entry, dict)
                for name, info in (entry.get("dependencies") or {}).items() if isinstance(info, dict)}
    if manager == "yarn":
        return _yarn_global()
    if manager == "brew":
        ok, output, _ = _capture(["brew", "list", "--versions"], timeout=_LIST_TIMEOUT_S)
        if not ok:
            return None
        return {line.split()[0]: line.split()[-1] for line in output.splitlines() if len(line.split()) >= 2}
    if manager.startswith("pip:"):
        program = shlex.split(manager[4:])
        ok, output, _ = _capture([*program, "list", "--format=json", "--disable-pip-version-check"],
                                 timeout=_LIST_TIMEOUT_S)
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
    elif manager in ("pnpm", "yarn"):
        remove, add = (["pnpm", "remove", "-g"], ["pnpm", "add", "-g"]) if manager == "pnpm" else \
            (["yarn", "global", "remove"], ["yarn", "global", "add"])
        if added:
            steps.append(shlex.join([*remove, *added]))
        if removed or changed:
            steps.append(shlex.join([*add, *(f"{n}@{before[n]}" for n in removed + changed)]))
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
