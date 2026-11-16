# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Dependency detection and restore with real package managers (design V8, V9).

Needs the network (PyPI) and runs only with ``RECOVERABLE_REAL_PACKAGES=1``.
Everything is installed into virtual environments under the test's temporary
directory; "global" pip is a virtual environment standing in for the system
Python, so nothing on the machine changes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from openjiuwen.harness.recoverable.dependencies import (
    diff_packages,
    fingerprint,
    global_manager,
    global_undo,
    list_global,
    list_packages,
    restore_environment,
)

UV = shutil.which("uv") or shutil.which(str(Path.home() / ".local" / "bin" / "uv"))

pytestmark = pytest.mark.skipif(
    os.environ.get("RECOVERABLE_REAL_PACKAGES") != "1", reason="needs PyPI; set RECOVERABLE_REAL_PACKAGES=1",
)


def _sh(*argv: str, cwd: Path | None = None, env: dict | None = None) -> str:
    completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


def _pip_env(path: Path) -> Path:
    venv.EnvBuilder(with_pip=True).create(path)
    return path


def _local_package(root: Path, name: str, version: str) -> Path:
    project = root / name
    (project / name).mkdir(parents=True)
    (project / name / "__init__.py").write_text(f"VERSION = {version!r}\n")
    (project / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"setuptools>=61\"]\nbuild-backend = \"setuptools.build_meta\"\n"
        f"[project]\nname = \"{name}\"\nversion = \"{version}\"\n"
    )
    return project


def test_pip_installs_upgrades_and_removals_are_seen_and_restored(tmp_path):
    env = _pip_env(tmp_path / ".venv")
    pip = str(env / "bin" / "pip")
    start, start_print = list_packages(env), fingerprint(env)

    _sh(pip, "install", "six==1.16.0")
    installed, installed_print = list_packages(env), fingerprint(env)
    _sh(pip, "install", "six==1.17.0")
    upgraded, upgraded_print = list_packages(env), fingerprint(env)

    assert installed["six"] == "1.16.0" and upgraded["six"] == "1.17.0"
    assert len({start_print, installed_print, upgraded_print}) == 3, "every change moves the fingerprint"

    # Back to the list with six 1.16.0, then to the start without six.
    assert restore_environment(tmp_path, diff_packages(".venv", upgraded, installed)) is None
    assert list_packages(env) == installed
    assert restore_environment(tmp_path, diff_packages(".venv", installed, start)) is None
    assert list_packages(env) == start


@pytest.mark.skipif(UV is None, reason="uv is not installed")
def test_a_uv_environment_without_pip_is_restored_with_uv(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(UV).parent}{os.pathsep}{os.environ['PATH']}")
    env = tmp_path / ".venv"
    _sh(UV, "venv", "--python", sys.executable, str(env))
    start = list_packages(env)
    _sh(UV, "pip", "install", "--python", str(env / "bin" / "python"), "six==1.16.0")
    installed = list_packages(env)

    assert installed["six"] == "1.16.0" and fingerprint(env) != ""
    assert restore_environment(tmp_path, diff_packages(".venv", installed, start)) is None
    assert list_packages(env) == start
    assert restore_environment(tmp_path, diff_packages(".venv", start, installed)) is None
    assert list_packages(env) == installed


def test_packages_installed_from_a_local_path_or_git_are_restored(tmp_path):
    env = _pip_env(tmp_path / ".venv")
    pip = str(env / "bin" / "pip")
    local = _local_package(tmp_path / "src", "localpkg", "0.1.0")
    repo = _local_package(tmp_path / "repo", "gitpkg", "0.2.0")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    _sh(*git, "init", "-q", cwd=repo)
    _sh(*git, "add", ".", cwd=repo)
    _sh(*git, "commit", "-q", "-m", "v0.2.0", cwd=repo)
    start = list_packages(env)

    _sh(pip, "install", str(local), f"git+file://{repo}")
    installed = list_packages(env)
    assert installed["localpkg"].startswith("0.1.0") and installed["gitpkg"].startswith("0.2.0")

    assert restore_environment(tmp_path, diff_packages(".venv", installed, start)) is None
    assert list_packages(env) == start
    error = restore_environment(tmp_path, diff_packages(".venv", start, installed))
    assert error is None, error
    assert list_packages(env) == installed


def test_an_editable_install_of_the_project_is_restored_as_editable(tmp_path):
    env = _pip_env(tmp_path / ".venv")
    project = _local_package(tmp_path, "myproject", "1.0.0")
    start = list_packages(env)
    _sh(str(env / "bin" / "pip"), "install", "-e", str(project))
    installed = list_packages(env)
    assert " @ editable file://" in installed["myproject"]

    assert restore_environment(tmp_path, diff_packages(".venv", installed, start)) is None
    assert restore_environment(tmp_path, diff_packages(".venv", start, installed)) is None
    assert list_packages(env) == installed, "reinstalled as editable from the same directory"


def test_a_global_pip_install_and_upgrade_are_reversed(tmp_path, monkeypatch):
    system = _pip_env(tmp_path / "system-python")  # Stands in for the machine's Python.
    _sh(str(system / "bin" / "pip"), "install", "six==1.16.0")
    monkeypatch.setenv("PATH", f"{system / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    command = "pip install six==1.17.0 tomli==2.0.1"
    manager = global_manager(command)
    assert manager == "pip:pip"

    before = list_global(manager)
    _sh("sh", "-c", command, env=os.environ.copy())
    after = list_global(manager)
    undo, cannot = global_undo(manager, before, after)

    assert after["six"] == "1.17.0" and "tomli" in after and cannot == []
    _sh("sh", "-c", undo, env=os.environ.copy())
    assert list_global(manager) == before
