# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""conda environments on rollback, without the network.

A fake ``conda`` on PATH keeps an environment as conda does on disk: one
``conda-meta/<name>-<version>-0.json`` per package and a ``conda-meta/history``
with one block per revision. ``--revision N`` rebuilds the packages of revision
N from the history. Named environments live under a fake root's ``envs/``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from openjiuwen.harness.recoverable import EffectAction, RollbackStatus
from openjiuwen.harness.recoverable.dependencies import global_manager, list_packages
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

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the fake conda is a script")

FAKE_CONDA = r"""
import json, os, shutil, sys
from pathlib import Path

args = sys.argv[1:]
root = Path(os.environ["FAKE_CONDA_ROOT"])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(["conda", *args]) + "\n")
if args[:1] == ["info"]:
    print(json.dumps({"root_prefix": str(root), "default_prefix": os.environ.get("CONDA_PREFIX") or str(root),
                      "envs_dirs": [str(root / "envs")]}))
    sys.exit(0)
verb, rest = args[0], args[1:]
prefix, revision, specs, everything = None, None, [], False
i = 0
while i < len(rest):
    word = rest[i]
    if word in ("-n", "-p", "--revision"):
        value = rest[i + 1]
        if word == "-n":
            prefix = root if value == "base" else root / "envs" / value
        elif word == "-p":
            prefix = Path(value)
        else:
            revision = int(value)
        i += 2
        continue
    if word == "--all":
        everything = True
    elif not word.startswith("-"):
        specs.append(word)
    i += 1
prefix = (prefix or Path(os.environ.get("CONDA_PREFIX") or root)).absolute()
meta = prefix / "conda-meta"


def installed():
    return {json.loads(p.read_text())["name"]: json.loads(p.read_text())["version"] for p in meta.glob("*.json")}


def states():
    blocks, state = [], {}
    for line in (meta / "history").read_text().splitlines():
        if line.startswith("==>"):
            blocks.append(dict(state))
        elif line[:1] in "+-" and "::" in line:
            name, version, _ = line[1:].split("::", 1)[1].rsplit("-", 2)
            state.pop(name, None) if line[0] == "-" else state.update({name: version})
            blocks[-1] = dict(state)
    return blocks


def write(packages):
    old = installed() if meta.exists() else {}
    meta.mkdir(parents=True, exist_ok=True)
    for path in meta.glob("*.json"):
        path.unlink()
    for name, version in packages.items():
        (meta / f"{name}-{version}-0.json").write_text(json.dumps({"name": name, "version": version}))
    lines = [f"-fake::{n}-{v}-0" for n, v in old.items() if packages.get(n) != v]
    lines += [f"+fake::{n}-{v}-0" for n, v in packages.items() if old.get(n) != v]
    with open(meta / "history", "a") as history:
        history.write("==> 2026-01-01 00:00:00 <==\n# cmd: conda " + " ".join(args) + "\n" + "".join(l + "\n" for l in lines))


if verb == "remove" and everything:
    shutil.rmtree(prefix)
elif verb == "remove":
    write({n: v for n, v in installed().items() if n not in specs})
elif revision is not None:
    write(installed() if os.environ.get("FAKE_CONDA_BROKEN_REVISION") else states()[revision])
elif verb in ("create", "install"):
    current = installed() if verb == "install" else {}
    write({**current, **dict(spec.split("=", 1) for spec in specs)})
else:
    sys.exit(1)
"""


@pytest.fixture
def fake_conda(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    _script(bin_dir / "conda", f"#!{sys.executable}\n{FAKE_CONDA}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("FAKE_CONDA_ROOT", str(tmp_path / "conda"))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    return tmp_path / "calls.log"


def _conda(*args: str) -> None:
    subprocess.run(["conda", *args], check=True)


def _bash(command: str, workdir: Path | None = None, call_id: str = "c1"):
    args = {"command": command, "side_effect": "none", **({"workdir": str(workdir)} if workdir else {})}
    return create_tool_call_response("bash", json.dumps(args), tool_call_id=call_id)


def _versions(prefix: Path) -> dict[str, str]:
    return {name: version for name, version in list_packages(prefix).items() if name}


@pytest.mark.asyncio
@pytest.mark.parametrize("dirname, absolute", [("env", True), (".venv", False)])
async def test_a_conda_env_in_the_workspace_goes_back_to_the_checkpoint_revision(
    tmp_path, runner, fake_conda, dirname, absolute,
):
    ws = tmp_path / "ws"
    ws.mkdir()
    prefix = ws / dirname
    _conda("create", "-p", str(prefix), "zlib=1.2.13")
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    where = str(prefix) if absolute else f"./{dirname}"
    with scripted(_model(_bash(f"conda install -p {where} -y zlib=1.3.1 xz=5.8.3", ws))):
        await run(agent, "upgrade zlib", "s1")
        assert _versions(prefix) == {"zlib": "1.3.1", "xz": "5.8.3"}
        plan, result = await _rollback(harness)

    assert [(c.env, c.remove, c.install, c.error) for c in plan.package_changes] == [
        (dirname, ["xz"], {"zlib": "1.2.13"}, None),
    ], "a conda env is a dependency environment whatever its name, and its revision is not a package"
    assert not [e for e in plan.effects if e.undo_args], "inside the workspace it is not also a global install"
    assert result.status == RollbackStatus.COMPLETED, result.package_changes
    assert _versions(prefix) == {"zlib": "1.2.13"}
    assert f"conda install -p {prefix} --revision 0 -y -q" in fake_conda.read_text()
    assert not [p for p in result.files_written if p.startswith(dirname)], "the env is not in the snapshot"


@pytest.mark.asyncio
async def test_a_conda_revision_that_leaves_a_difference_is_reported(tmp_path, runner, fake_conda, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _conda("create", "-p", str(ws / "env"), "zlib=1.2.13")
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    with scripted(_model(_bash("conda install -p ./env -y xz=5.8.3", ws))):
        await run(agent, "add xz", "s1")
        monkeypatch.setenv("FAKE_CONDA_BROKEN_REVISION", "1")
        _, result = await _rollback(harness)

    assert result.status == RollbackStatus.PARTIAL
    [change] = result.package_changes
    assert "left it different from the target: remove xz" in (change.error or "")


@pytest.mark.asyncio
async def test_a_missing_conda_is_named_in_the_plan(tmp_path, runner, monkeypatch):
    monkeypatch.setenv("PATH", f"/usr/bin{os.pathsep}/bin")  # No conda, no mamba.
    ws = tmp_path / "ws"
    meta = ws / "env" / "conda-meta"
    meta.mkdir(parents=True)
    (meta / "history").write_text("==> 2026-01-01 00:00:00 <==\n+fake::zlib-1.2.13-0\n")
    (meta / "zlib-1.2.13-0.json").write_text('{"name": "zlib", "version": "1.2.13"}')
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    add = "echo '{\"name\": \"xz\", \"version\": \"5.8.3\"}' > env/conda-meta/xz-5.8.3-0.json"
    with scripted(_model(_bash(add, ws))):
        await run(agent, "add xz", "s1")
        plan, result = await _rollback(harness)

    assert [c.error for c in plan.package_changes] == ["neither conda nor mamba is available"]
    assert result.status == RollbackStatus.PARTIAL


@pytest.mark.asyncio
async def test_a_named_conda_env_outside_the_workspace_is_reverted(tmp_path, runner, fake_conda):
    _conda("create", "-n", "myenv", "zlib=1.2.13")
    prefix = tmp_path / "conda" / "envs" / "myenv"
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    with scripted(_model(_bash("conda install -n myenv -y zlib=1.3.1 xz=5.8.3"))):
        await run(agent, "upgrade zlib", "s1")
        plan, result = await _rollback(harness)

    [effect] = plan.effects
    assert effect.undo_args == {"command": f"conda install -p {prefix} --revision 0 -y"}
    assert result.status == RollbackStatus.COMPLETED
    assert _versions(prefix) == {"zlib": "1.2.13"}


@pytest.mark.asyncio
async def test_an_env_the_agent_created_is_removed_and_one_it_deleted_is_reported(tmp_path, runner, fake_conda):
    _conda("create", "-n", "old", "zlib=1.2.13")
    envs = tmp_path / "conda" / "envs"
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    llm = _model(_bash("conda create -n made -y zlib=1.3.1", call_id="c1"),
                 _bash("conda remove -n old --all -y", call_id="c2"))
    with scripted(llm):
        await run(agent, "swap environments", "s1")
        assert (envs / "made").is_dir() and not (envs / "old").exists()
        plan, result = await _rollback(harness)

    assert [e.undo_args for e in plan.effects if e.undo_args] == [
        {"command": f"conda remove -p {envs / 'made'} --all -y"},
    ]
    [cannot] = [e for e in plan.effects if e.action == EffectAction.CANNOT_UNDO]
    assert cannot.args_summary == f"the conda environment {envs / 'old'} was deleted"
    assert not (envs / "made").exists()
    assert result.status == RollbackStatus.COMPLETED


@pytest.mark.parametrize("command, manager", [
    ("conda install -n myenv zlib", "conda:-n myenv"),
    ("mamba install --prefix=/opt/env zlib", "conda:-p /opt/env"),
    ("conda update --all", "conda:"),
    ("conda env remove --name old", "conda:-n old"),
    ("conda create zlib", None),
    ("conda env create -f environment.yml", None),
    ("conda list", None),
    ("conda run -n myenv pip install six", None),
])
def test_conda_commands_are_recognized(command, manager):
    assert global_manager(command) == manager
