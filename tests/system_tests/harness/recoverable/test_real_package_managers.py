# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Dependency detection and restore with real package managers (design V8, V9).

Needs the network (PyPI, the npm registry, Yarn releases, conda-forge) and runs
only with ``RECOVERABLE_REAL_PACKAGES=1``; the npm, pnpm, yarn and conda tests are
skipped when those programs are on neither PATH nor ``~/.local/node/bin`` /
``~/.local/miniforge3/bin``. Everything is installed under the test's temporary
directory: "global" pip is a virtual environment standing in for the system
Python, and the global packages, stores and caches of npm, pnpm and yarn and
conda's named environments are pointed there. Only conda's package cache, in
conda's own install directory, is shared between runs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from openjiuwen.harness.recoverable import RollbackStatus
from openjiuwen.harness.recoverable.dependencies import (
    diff_packages,
    fingerprint,
    global_manager,
    global_undo,
    list_global,
    list_packages,
    restore_environment,
)
from tests.unit_tests.fixtures.deep_agent_interrupts import run, scripted
from tests.unit_tests.fixtures.mock_llm import create_tool_call_response
from tests.unit_tests.harness.recoverable.test_dependencies import (  # noqa: F401
    _agent,
    _harness,
    _model,
    _rollback,
    runner,
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


# --------------------------------------------------------------------- npm

NODE_BIN = Path.home() / ".local" / "node" / "bin"
needs_npm = pytest.mark.skipif(
    shutil.which("npm") is None and not (NODE_BIN / "npm").exists(), reason="npm is not installed",
)


@pytest.fixture
def npm_path(monkeypatch):
    if shutil.which("npm") is None:
        monkeypatch.setenv("PATH", f"{NODE_BIN}{os.pathsep}{os.environ['PATH']}")
    return shutil.which("npm")


def _npm_project(path: Path) -> Path:
    path.mkdir(parents=True)
    _sh("npm", "init", "-y", cwd=path)
    return path


@needs_npm
@pytest.mark.asyncio
async def test_an_npm_project_returns_to_its_checkpoint(tmp_path, npm_path, runner):
    ws = tmp_path / "ws"
    web = _npm_project(ws / "web")
    _sh("npm", "install", "left-pad@1.1.0", "--no-fund", "--no-audit", cwd=web)
    start = list_packages(web / "node_modules")
    files = {name: (web / name).read_text() for name in ("package.json", "package-lock.json")}
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    # Saved to package.json, as npm does by default.
    bash = json.dumps({"command": "npm install left-pad@1.3.0 is-odd@3.0.1 --no-fund --no-audit",
                       "workdir": str(web), "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", bash, tool_call_id="c1"))):
        await run(agent, "upgrade", "s1")
        assert list_packages(web / "node_modules")["left-pad"] == "1.3.0"
        _, result = await _rollback(harness)

    assert result.status == RollbackStatus.COMPLETED, result.package_changes
    assert list_packages(web / "node_modules") == start, "is-odd and its dependency are gone, left-pad is 1.1.0"
    assert {name: (web / name).read_text() for name in files} == files


@needs_npm
def test_npm_packages_from_a_local_path_or_git_are_restored_from_there(tmp_path, npm_path):
    web = _npm_project(tmp_path / "web")
    _sh("npm", "install", "left-pad@1.1.0", "--no-fund", "--no-audit", cwd=web)
    local = _npm_project(tmp_path / "localpkg")
    repo = _npm_project(tmp_path / "repo")
    package = json.loads((repo / "package.json").read_text())
    (repo / "package.json").write_text(json.dumps({**package, "name": "gitpkg"}))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    _sh(*git, "init", "-q", cwd=repo)
    _sh(*git, "add", ".", cwd=repo)
    _sh(*git, "commit", "-q", "-m", "v1", cwd=repo)
    start = list_packages(web / "node_modules")
    _sh("npm", "install", "--no-save", "--no-fund", "--no-audit", str(local), f"git+file://{repo}", cwd=web)
    installed = list_packages(web / "node_modules")
    assert " @ " in installed["localpkg"] and installed["gitpkg"].split(" @ ")[1].startswith("git+file://")

    assert restore_environment(tmp_path, diff_packages("web/node_modules", installed, start)) is None
    assert list_packages(web / "node_modules") == start
    error = restore_environment(tmp_path, diff_packages("web/node_modules", start, installed))
    assert error is None, error
    assert list_packages(web / "node_modules") == installed


@needs_npm
@pytest.mark.asyncio
async def test_a_global_npm_install_and_upgrade_are_reversed(tmp_path, npm_path, runner, monkeypatch):
    monkeypatch.setenv("NPM_CONFIG_PREFIX", str(tmp_path / "npm-global"))  # Not the machine's global packages.
    _sh("npm", "install", "-g", "left-pad@1.1.0", "--no-fund", "--no-audit")
    before = list_global("npm")
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    bash = json.dumps({"command": "npm install -g left-pad@1.3.0 is-odd@3.0.1 --no-fund --no-audit",
                       "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", bash, tool_call_id="c1"))):
        await run(agent, "install tools", "s1")
        assert list_global("npm")["left-pad"] == "1.3.0"
        _, result = await _rollback(harness)

    assert result.status == RollbackStatus.COMPLETED, result.effects
    assert list_global("npm") == before


# --------------------------------------------------------------------- pnpm and yarn

LOCKFILES = {"pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}
ADD = {"pnpm": ["pnpm", "add"], "yarn": ["yarn", "add", "--no-progress"]}
GLOBAL_ADD = {"pnpm": "pnpm add -g", "yarn": "yarn global add --no-progress"}


@pytest.fixture
def node_managers(tmp_path, monkeypatch):
    """pnpm and yarn with their store, caches and global packages under the test's directory."""
    pnpm_home = tmp_path / "pnpm-home"
    monkeypatch.setenv("PATH", os.pathsep.join([str(pnpm_home / "bin"), str(NODE_BIN), os.environ["PATH"]]))
    for name in ("pnpm", "yarn"):
        if shutil.which(name) is None:
            pytest.skip(f"{name} is not installed")
    monkeypatch.setenv("PNPM_HOME", str(pnpm_home))
    monkeypatch.setenv("pnpm_config_store_dir", str(tmp_path / "pnpm-store"))
    monkeypatch.setenv("pnpm_config_cache_dir", str(tmp_path / "pnpm-cache"))
    monkeypatch.setenv("YARN_CACHE_FOLDER", str(tmp_path / "yarn-cache"))
    monkeypatch.setenv("YARN_GLOBAL_FOLDER", str(tmp_path / "yarn-global"))
    monkeypatch.setenv("YARN_PREFIX", str(tmp_path / "yarn-prefix"))


def _node_project(path: Path, manager: str, *packages: str) -> Path:
    path.mkdir(parents=True)
    (path / "package.json").write_text(json.dumps({"name": path.name, "version": "1.0.0", "private": True}))
    if packages:
        _sh(*ADD[manager], *packages, cwd=path)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("manager", ["pnpm", "yarn"])
async def test_a_pnpm_or_yarn_project_returns_to_its_checkpoint(tmp_path, node_managers, runner, manager):
    ws = tmp_path / "ws"
    web = _node_project(ws / "web", manager, "left-pad@1.1.0")
    start = list_packages(web / "node_modules")
    files = {name: (web / name).read_text() for name in ("package.json", LOCKFILES[manager])}
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    command = " ".join([*ADD[manager], "left-pad@1.3.0", "is-odd@3.0.1"])
    bash = json.dumps({"command": command, "workdir": str(web), "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", bash, tool_call_id="c1"))):
        await run(agent, "upgrade", "s1")
        assert list_packages(web / "node_modules")["left-pad"] == "1.3.0"
        plan, result = await _rollback(harness)

    assert [c.error for c in plan.package_changes] == [None]
    assert result.status == RollbackStatus.COMPLETED, result.package_changes
    assert list_packages(web / "node_modules") == start, "is-odd is gone and left-pad is 1.1.0"
    assert {name: (web / name).read_text() for name in files} == files


@pytest.mark.asyncio
@pytest.mark.parametrize("manager", ["pnpm", "yarn"])
async def test_a_global_pnpm_or_yarn_install_and_upgrade_are_reversed(tmp_path, node_managers, runner, manager):
    _sh("sh", "-c", f"{GLOBAL_ADD[manager]} left-pad@1.1.0", env=os.environ.copy())
    before = list_global(manager)
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    bash = json.dumps({"command": f"{GLOBAL_ADD[manager]} left-pad@1.3.0 is-odd@3.0.1", "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", bash, tool_call_id="c1"))):
        await run(agent, "install tools", "s1")
        assert list_global(manager)["left-pad"] == "1.3.0" and "is-odd" in list_global(manager)
        _, result = await _rollback(harness)

    assert before == {"left-pad": "1.1.0"}
    assert result.status == RollbackStatus.COMPLETED, result.effects
    assert list_global(manager) == before


@pytest.mark.asyncio
async def test_a_yarn_plug_and_play_project_returns_with_its_files(tmp_path, node_managers, runner, monkeypatch):
    """Yarn 2 and later resolve packages through ``.pnp.cjs`` and ``yarn.lock``, which the snapshot restores."""
    monkeypatch.delenv("YARN_PREFIX")  # A yarn 1 setting; later versions refuse to run with it.
    ws = tmp_path / "ws"
    web = _node_project(ws / "web", "yarn")
    _sh("yarn", "set", "version", "4.9.2", cwd=web)
    _sh("yarn", "add", "left-pad@1.1.0", cwd=web)

    def resolved() -> str:
        return _sh("node", "-r", str(web / ".pnp.cjs"), "-p", "require('left-pad/package.json').version", cwd=web)

    assert resolved().strip() == "1.1.0" and not (web / "node_modules").exists()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    bash = json.dumps({"command": "yarn add left-pad@1.3.0 is-odd@3.0.1", "workdir": str(web), "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", bash, tool_call_id="c1"))):
        await run(agent, "upgrade", "s1")
        assert resolved().strip() == "1.3.0"
        _, result = await _rollback(harness)

    assert result.status == RollbackStatus.COMPLETED
    assert resolved().strip() == "1.1.0"


# --------------------------------------------------------------------- conda

CONDA_BIN = Path.home() / ".local" / "miniforge3" / "bin"


@pytest.fixture
def conda(tmp_path, monkeypatch):
    """conda with its named environments under the test's directory; returns that directory.

    conda's package cache in its own install directory is reused, or every test would download the index again.
    """
    monkeypatch.setenv("PATH", os.pathsep.join([str(CONDA_BIN), os.environ["PATH"]]))
    if shutil.which("conda") is None:
        pytest.skip("conda is not installed")
    monkeypatch.setenv("CONDA_ENVS_PATH", str(tmp_path / "conda-envs"))
    monkeypatch.setenv("CONDA_REGISTER_ENVS", "false")  # Leaves ~/.conda/environments.txt alone.
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    return tmp_path / "conda-envs"


def _conda_versions(prefix: Path) -> dict[str, str]:
    return {name: version for name, version in list_packages(prefix).items() if name}


@pytest.mark.asyncio
async def test_a_conda_env_in_the_workspace_returns_to_its_revision(tmp_path, conda, runner):
    ws = tmp_path / "ws"
    ws.mkdir()
    prefix = ws / "env"
    _sh("conda", "create", "-p", str(prefix), "-y", "-q", "zlib=1.2.13")
    start = _conda_versions(prefix)
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    bash = json.dumps({"command": "conda install -p ./env -y -q zlib=1.3.1 xz", "workdir": str(ws),
                       "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", bash, tool_call_id="c1"))):
        await run(agent, "upgrade zlib", "s1")
        assert _conda_versions(prefix)["zlib"] == "1.3.1" and "xz" in _conda_versions(prefix)
        plan, result = await _rollback(harness)

    assert [c.error for c in plan.package_changes] == [None]
    assert result.status == RollbackStatus.COMPLETED, result.package_changes
    assert _conda_versions(prefix) == start, "zlib is 1.2.13 again and xz is gone"


@pytest.mark.asyncio
async def test_a_named_conda_env_is_reverted_and_one_the_agent_made_is_removed(tmp_path, conda, runner):
    _sh("conda", "create", "-n", "kept", "-y", "-q", "zlib=1.2.13")
    start = _conda_versions(conda / "kept")
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    upgrade = json.dumps({"command": "conda install -n kept -y -q zlib=1.3.1", "side_effect": "none"})
    create = json.dumps({"command": "conda create -n made -y -q zlib", "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", upgrade, tool_call_id="c1"),
                         create_tool_call_response("bash", create, tool_call_id="c2"))):
        await run(agent, "set up environments", "s1")
        assert _conda_versions(conda / "kept")["zlib"] == "1.3.1" and (conda / "made").is_dir()
        _, result = await _rollback(harness)

    assert result.status == RollbackStatus.COMPLETED, result.effects
    assert _conda_versions(conda / "kept") == start
    assert not (conda / "made").exists()
