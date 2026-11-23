# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""pnpm and yarn projects and their global installs on rollback, without the network.

A fake ``pnpm`` / ``yarn`` on PATH keeps the lockfile as ``name version`` lines:
``add`` installs into ``node_modules`` and writes the lockfile, a frozen-lockfile
``install`` rebuilds ``node_modules`` from the lockfile alone. Global installs go
to a folder with a ``package.json``, as yarn keeps them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from openjiuwen.harness.recoverable import RollbackStatus
from openjiuwen.harness.recoverable.dependencies import global_manager, list_global, list_packages
from tests.unit_tests.fixtures.deep_agent_interrupts import run, scripted
from tests.unit_tests.fixtures.mock_llm import create_tool_call_response
from tests.unit_tests.harness.recoverable.test_dependencies import (  # noqa: F401
    _agent,
    _harness,
    _model,
    _rollback,
    _script,
    runner,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake package managers are scripts")

FAKE_NODE_MANAGER = r"""
import json, os, sys
from pathlib import Path

manager, args = Path(sys.argv[0]).name, sys.argv[1:]
lock = Path("pnpm-lock.yaml" if manager == "pnpm" else "yarn.lock")
folder = Path(os.environ["FAKE_GLOBAL"])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join([manager, *args]) + "\n")


def put(modules, name, version):
    (modules / name).mkdir(parents=True, exist_ok=True)
    (modules / name / "package.json").write_text(json.dumps({"name": name, "version": version}))


def specs(words):
    return [w.rsplit("@", 1) for w in words if not w.startswith("-")]


def read_global():
    path = folder / "package.json"
    return json.loads(path.read_text())["dependencies"] if path.exists() else {}


def write_global(deps):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "package.json").write_text(json.dumps({"dependencies": deps}))
    for name, version in deps.items():
        put(folder / "node_modules", name, version)


words = [w for w in args if w not in ("-g", "--global")]
is_global = manager == "yarn" and args[:1] == ["global"] or "-g" in args or "--global" in args
if manager == "yarn" and args[:1] == ["global"]:
    words = words[1:]
verb, rest = words[0], words[1:]
if is_global:
    deps = read_global()
    if verb == "dir":
        print(folder)
    elif verb == "ls":
        print(json.dumps([{"dependencies": {n: {"version": v} for n, v in deps.items()}}]))
    elif verb == "add":
        write_global({**deps, **dict(specs(rest))})
    elif verb == "remove":
        write_global({n: v for n, v in deps.items() if n not in rest})
    sys.exit(0)
if verb == "add":
    entries = dict(line.split() for line in lock.read_text().splitlines()) if lock.exists() else {}
    for name, version in specs(rest):
        entries[name] = version
        put(Path("node_modules"), name, version)
    lock.write_text("".join(f"{n} {v}\n" for n, v in sorted(entries.items())))
elif verb == "install" and ("--frozen-lockfile" in rest or "--immutable" in rest):
    for entry in Path("node_modules").glob("[!.]*"):
        for path in sorted(entry.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        entry.rmdir()
    for line in lock.read_text().splitlines():
        put(Path("node_modules"), *line.split())
else:
    sys.exit(1)
"""

LOCKFILES = {"pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}
GLOBAL_ADD = {"pnpm": "pnpm add -g", "yarn": "yarn global add"}


@pytest.fixture
def fake_managers(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    for name in LOCKFILES:
        _script(bin_dir / name, f"#!{sys.executable}\n{FAKE_NODE_MANAGER}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("FAKE_GLOBAL", str(tmp_path / "global"))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    return tmp_path / "calls.log"


def _project(ws: Path, manager: str, *packages: str) -> Path:
    web = ws / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"name": "web"}')
    subprocess.run([manager, "add", *packages], cwd=web, check=True)
    return web


def _bash(command: str, workdir: Path | None = None):
    args = {"command": command, "side_effect": "none", **({"workdir": str(workdir)} if workdir else {})}
    return create_tool_call_response("bash", json.dumps(args), tool_call_id="c1")


@pytest.mark.asyncio
@pytest.mark.parametrize("manager", ["pnpm", "yarn"])
async def test_a_pnpm_or_yarn_project_is_installed_from_the_target_lockfile(tmp_path, runner, fake_managers, manager):
    ws = tmp_path / "ws"
    web = _project(ws, manager, "left-pad@1.0.0")
    lockfile = (web / LOCKFILES[manager]).read_text()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    with scripted(_model(_bash(f"{manager} add left-pad@1.3.0 is-odd@3.0.1", web))):
        await run(agent, "upgrade", "s1")
        assert list_packages(web / "node_modules") == {"left-pad": "1.3.0", "is-odd": "3.0.1"}
        plan, result = await _rollback(harness)

    assert [(c.env, c.remove, c.install, c.error) for c in plan.package_changes] == [
        ("web/node_modules", ["is-odd"], {"left-pad": "1.0.0"}, None),
    ], "the plan says it will restore them"
    assert result.status == RollbackStatus.COMPLETED, result.package_changes
    assert list_packages(web / "node_modules") == {"left-pad": "1.0.0"}
    assert (web / LOCKFILES[manager]).read_text() == lockfile
    assert f"{manager} install --frozen-lockfile" in fake_managers.read_text(), "installed from the lockfile"


@pytest.mark.asyncio
async def test_a_package_the_lockfile_does_not_name_is_reported(tmp_path, runner, fake_managers):
    ws = tmp_path / "ws"
    web = _project(ws, "pnpm", "left-pad@1.0.0")
    (web / "node_modules/local-tool").mkdir()  # Copied in by hand; no lockfile names it.
    (web / "node_modules/local-tool/package.json").write_text('{"name": "local-tool", "version": "0.1.0"}')
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    with scripted(_model(_bash("pnpm add is-odd@3.0.1", web))):
        await run(agent, "add is-odd", "s1")
        _, result = await _rollback(harness)

    assert result.status == RollbackStatus.PARTIAL
    [change] = result.package_changes
    assert "left it different from the target: install local-tool==0.1.0" in (change.error or "")


@pytest.mark.asyncio
async def test_a_missing_package_manager_is_named_in_the_plan_and_nothing_runs(tmp_path, runner, monkeypatch):
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}/bin")  # No pnpm.
    ws = tmp_path / "ws"
    web = ws / "web"
    (web / "node_modules/left-pad").mkdir(parents=True)
    (web / "package.json").write_text('{"name": "web"}')
    (web / "pnpm-lock.yaml").write_text("left-pad 1.0.0\n")
    (web / "node_modules/left-pad/package.json").write_text('{"name": "left-pad", "version": "1.0.0"}')
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    install = ("mkdir -p node_modules/is-odd && "
               "echo '{\"name\": \"is-odd\", \"version\": \"3.0.1\"}' > node_modules/is-odd/package.json")
    with scripted(_model(_bash(install, web))):
        await run(agent, "add is-odd", "s1")
        plan, result = await _rollback(harness)

    assert [c.error for c in plan.package_changes] == ["pnpm is not available"], "known before the confirmation"
    assert result.status == RollbackStatus.PARTIAL
    assert "is-odd" in list_packages(web / "node_modules")


@pytest.mark.asyncio
@pytest.mark.parametrize("manager", ["pnpm", "yarn"])
async def test_a_global_pnpm_or_yarn_install_is_reversed(tmp_path, runner, fake_managers, manager):
    subprocess.run([*GLOBAL_ADD[manager].split(), "kept@2.0.0", "typescript@5.0.0"], check=True)
    before = list_global(manager)
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    with scripted(_model(_bash(f"{GLOBAL_ADD[manager]} eslint@9.0.0 typescript@5.4.0"))):
        await run(agent, "install tools", "s1")
        assert list_global(manager)["typescript"] == "5.4.0"
        plan, result = await _rollback(harness)

    remove = "pnpm remove -g" if manager == "pnpm" else "yarn global remove"
    [effect] = plan.effects
    assert effect.undo_args == {"command": f"{remove} eslint && {GLOBAL_ADD[manager]} typescript@5.0.0"}
    assert result.status == RollbackStatus.COMPLETED
    assert list_global(manager) == before == {"kept": "2.0.0", "typescript": "5.0.0"}


@pytest.mark.parametrize("command, manager", [
    ("pnpm add -g left-pad", "pnpm"),
    ("pnpm remove --global left-pad", "pnpm"),
    ("pnpm i -g left-pad@1.3.0", "pnpm"),
    ("yarn global add left-pad", "yarn"),
    ("yarn global upgrade", "yarn"),
    ("pnpm add left-pad", None),
    ("yarn add left-pad", None),
    ("yarn global dir", None),
])
def test_global_pnpm_and_yarn_commands_are_recognized(command, manager):
    assert global_manager(command) == manager
