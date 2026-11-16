# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Dependency environments and global installs on rollback, without the network.

A fake virtual environment's ``bin/python`` is a shell script that implements
``-m pip install`` / ``uninstall`` by writing ``*.dist-info`` directories; a fake
``npm`` on PATH does the same for ``node_modules`` and for a directory standing
in for the global packages.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperationCard
from openjiuwen.harness.factory import create_deep_agent
from openjiuwen.harness.rails import SysOperationRail
from openjiuwen.harness.recoverable import (
    EffectCategory,
    EffectDeclaration,
    RecoverableHarness,
    RollbackApproval,
    RollbackStatus,
)
from openjiuwen.harness.recoverable.dependencies import list_packages
from tests.unit_tests.fixtures.deep_agent_interrupts import fake_model, messages_seen, run, scripted
from tests.unit_tests.fixtures.mock_llm import MockLLMModel, create_text_response, create_tool_call_response

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake package managers are shell scripts")

FAKE_PIP = r"""#!/bin/sh
SP="$(cd "$(dirname "$0")/.." && pwd)/lib/python3.11/site-packages"
[ "$1" = "-m" ] && [ "$2" = "pip" ] || exit 1
shift 2
case "$1" in
  --version) echo "pip 0 (fake)"; exit 0 ;;
  uninstall) shift
    for a in "$@"; do [ "$a" = "-y" ] && continue; rm -rf "$SP/$a"-*.dist-info; done; exit 0 ;;
  install) shift
    for spec in "$@"; do
      name="${spec%%==*}"; ver="${spec##*==}"
      [ "$name" = "broken" ] && { echo "no such version" >&2; exit 1; }
      rm -rf "$SP/$name"-*.dist-info; d="$SP/$name-$ver.dist-info"; mkdir -p "$d"
      printf 'Metadata-Version: 2.1\nName: %s\nVersion: %s\n' "$name" "$ver" > "$d/METADATA"
    done; exit 0 ;;
esac
exit 1
"""

FAKE_NPM = r"""#!/bin/sh
G="$FAKE_NPM_GLOBAL"
cmd="$1"; shift
global=0
case "$cmd" in
  ls)
    printf '{"dependencies":{'; first=1
    for f in "$G"/*; do [ -f "$f" ] || continue
      [ $first = 1 ] || printf ','; printf '"%s":{"version":"%s"}' "$(basename "$f")" "$(cat "$f")"; first=0
    done; printf '}}\n'; exit 0 ;;
  install|i)
    for a in "$@"; do case "$a" in -g|--global) global=1 ;; esac; done
    for s in "$@"; do case "$s" in -*) continue ;; esac
      n="${s%@*}"; v="${s##*@}"; [ "$n" = "$s" ] && v="1.0.0"
      if [ $global = 1 ]; then echo "$v" > "$G/$n"
      else mkdir -p "node_modules/$n"; printf '{"name":"%s","version":"%s"}' "$n" "$v" > "node_modules/$n/package.json"; fi
    done; exit 0 ;;
  uninstall|remove|rm)
    for a in "$@"; do case "$a" in -g|--global) global=1 ;; esac; done
    for a in "$@"; do case "$a" in -*) continue ;; esac
      if [ $global = 1 ]; then rm -f "$G/$a"; else rm -rf "node_modules/$a"; fi
    done; exit 0 ;;
esac
exit 1
"""


@pytest_asyncio.fixture
async def runner():
    await Runner.start()
    try:
        yield
    finally:
        await Runner.stop()


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _venv(root: Path, packages: dict[str, str]) -> Path:
    venv = root / ".venv"
    (venv / "lib/python3.11/site-packages").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    python = _script(venv / "bin/python", FAKE_PIP)
    if packages:
        subprocess.run([str(python), "-m", "pip", "install", *(f"{n}=={v}" for n, v in packages.items())], check=True)
    return python


class Pip(Tool):
    """Runs pip of the workspace's virtual environment, like ``.venv/bin/python -m pip ...`` in a shell."""

    def __init__(self, root: Path):
        super().__init__(ToolCard(id="pip", name="pip", description="pip",
                                  input_params={"type": "object", "properties": {"args": {"type": "array"}}}))
        self.root = root

    async def invoke(self, inputs, **kwargs):
        subprocess.run([str(self.root / ".venv/bin/python"), "-m", "pip", *inputs["args"]], check=True)
        return {"ok": True}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


def _agent(ws: Path, harness: RecoverableHarness, tools=()):
    card = SysOperationCard(
        id=f"recoverable_deps_{uuid.uuid4().hex[:8]}", mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(work_dir=str(ws), shell_allowlist=None),
    )
    Runner.resource_mgr.add_sys_operation(card)
    return create_deep_agent(
        model=fake_model(), card=AgentCard(id=f"deps_{uuid.uuid4().hex[:8]}", name="RecoverableDeps"),
        workspace=str(ws), sys_operation=Runner.resource_mgr.get_sys_operation(card.id),
        rails=[SysOperationRail(), harness.rail()], tools=list(tools),
        enable_task_loop=False, max_iterations=10, system_prompt="Follow the scripted model.",
    )


def _harness(tmp_path) -> RecoverableHarness:
    none = EffectDeclaration(category=EffectCategory.NONE)
    return RecoverableHarness(
        kv_store=InMemoryKVStore(), storage_dir=tmp_path / "recoverable",
        declarations={"pip": none, "make_venv": none},
    )


def _model(*responses) -> MockLLMModel:
    llm = MockLLMModel()
    llm.set_responses(list(responses) + [create_text_response("done"), create_text_response("again")])
    return llm


async def _rollback(harness, **choices):
    target = next(c for c in await harness.history("s1") if c.seq == 0)
    plan = await harness.plan_rollback("s1", target.checkpoint_id)
    result = await harness.apply_rollback(
        RollbackApproval(plan_id=plan.plan_id, target_id=plan.target_id, **choices),
    )
    return plan, result


@pytest.mark.asyncio
async def test_a_virtual_environment_returns_to_the_checkpoint_list(tmp_path, runner):
    ws = tmp_path / "ws"
    python = _venv(ws, {"foo": "1.0"})
    harness = _harness(tmp_path)
    agent = _agent(ws, harness, [Pip(ws)])
    llm = _model(create_tool_call_response("pip", json.dumps({"args": ["install", "bar==2.0", "foo==1.1"]}),
                                           tool_call_id="c1"))
    with scripted(llm):
        await run(agent, "install", "s1")
        checkpoints = list(reversed(await harness.history("s1")))
        assert checkpoints[-1].packages_id != checkpoints[0].packages_id, "the install step recorded a new list"
        subprocess.run([str(python), "-m", "pip", "install", "baz==3.0"], check=True)  # The user installs one too.
        plan, result = await _rollback(harness)
        await run(agent, "again", "s1")

    [change] = plan.package_changes
    assert (change.env, change.remove, change.install) == (".venv", ["bar", "baz"], {"foo": "1.0"})
    assert result.status == RollbackStatus.COMPLETED and result.package_changes[0].error is None
    assert list_packages(ws / ".venv") == {"foo": "1.0"}, "the user's own install is rolled back too"
    contents = [str(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in messages_seen(llm)]
    note = next(text for text in contents if text.startswith("[ROLLBACK]"))
    assert "Dependencies in .venv restored" in note


class MakeVenv(Tool):
    """Creates a virtual environment, like ``python -m venv .venv`` in a shell."""

    def __init__(self, root: Path):
        super().__init__(ToolCard(id="make_venv", name="make_venv", description="make a venv",
                                  input_params={"type": "object", "properties": {}}))
        self.root = root

    async def invoke(self, inputs, **kwargs):
        _venv(self.root, {"foo": "1.0"})
        return {"ok": True}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


@pytest.mark.asyncio
async def test_an_environment_made_after_the_checkpoint_is_deleted(tmp_path, runner):
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness, [MakeVenv(ws)])
    with scripted(_model(create_tool_call_response("make_venv", "{}", tool_call_id="c1"))):
        await run(agent, "make an environment", "s1")
        assert (ws / ".venv").is_dir()
        plan, result = await _rollback(harness)

    assert [(c.env, c.remove) for c in plan.package_changes] == [(".venv", ["(the whole environment)"])]
    assert not (ws / ".venv").exists()
    assert result.status == RollbackStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_failed_restore_leaves_the_rollback_partial(tmp_path, runner):
    ws = tmp_path / "ws"
    _venv(ws, {})
    # The fake pip refuses to install `broken`, as when a version is gone from the index.
    (ws / ".venv/lib/python3.11/site-packages/broken-1.0.dist-info").mkdir()
    (ws / ".venv/lib/python3.11/site-packages/broken-1.0.dist-info/METADATA").write_text(
        "Metadata-Version: 2.1\nName: broken\nVersion: 1.0\n")
    harness = _harness(tmp_path)
    agent = _agent(ws, harness, [Pip(ws)])
    with scripted(_model(create_tool_call_response("pip", json.dumps({"args": ["uninstall", "-y", "broken"]}),
                                                   tool_call_id="c1"))):
        await run(agent, "remove it", "s1")
        _, result = await _rollback(harness)

    assert result.status == RollbackStatus.PARTIAL
    [change] = result.package_changes
    assert change.install == {"broken": "1.0"} and "no such version" in (change.error or "")


@pytest.mark.asyncio
async def test_node_modules_of_an_npm_project_are_restored(tmp_path, runner, monkeypatch):
    bin_dir = tmp_path / "bin"
    _script(bin_dir / "npm", FAKE_NPM)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_NPM_GLOBAL", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    ws = tmp_path / "ws"
    (ws / "web").mkdir(parents=True)
    (ws / "web/package.json").write_text('{"name": "web"}')
    (ws / "web/package-lock.json").write_text("{}")
    subprocess.run(["npm", "install", "left-pad@1.0.0"], cwd=ws / "web", check=True)
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    bash = json.dumps({"command": "npm install left-pad@1.3.0 is-odd@3.0.1", "workdir": str(ws / "web"),
                       "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", bash, tool_call_id="c1"))):
        await run(agent, "upgrade", "s1")
        assert list_packages(ws / "web/node_modules") == {"left-pad": "1.3.0", "is-odd": "3.0.1"}
        plan, _ = await _rollback(harness)

    assert [(c.env, c.remove, c.install) for c in plan.package_changes] == [
        ("web/node_modules", ["is-odd"], {"left-pad": "1.0.0"}),
    ]
    assert list_packages(ws / "web/node_modules") == {"left-pad": "1.0.0"}


@pytest.mark.asyncio
async def test_a_global_install_by_the_agent_is_reversed(tmp_path, runner, monkeypatch):
    bin_dir, global_dir = tmp_path / "bin", tmp_path / "global"
    _script(bin_dir / "npm", FAKE_NPM)
    global_dir.mkdir()
    (global_dir / "kept").write_text("2.0.0")  # Installed by the user before; it stays.
    (global_dir / "typescript").write_text("5.0.0")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_NPM_GLOBAL", str(global_dir))
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = _harness(tmp_path)
    agent = _agent(ws, harness)
    bash = json.dumps({"command": "npm install -g eslint@9.0.0 typescript@5.4.0", "side_effect": "none"})
    with scripted(_model(create_tool_call_response("bash", bash, tool_call_id="c1"))):
        await run(agent, "install tools", "s1")
        plan, result = await _rollback(harness)

    [effect] = plan.effects
    assert effect.undo_args == {"command": "npm uninstall -g eslint && npm install -g typescript@5.0.0"}
    assert result.status == RollbackStatus.COMPLETED
    assert sorted(p.name for p in global_dir.iterdir()) == ["kept", "typescript"]
    assert (global_dir / "typescript").read_text().strip() == "5.0.0", "the upgrade is undone"


def test_a_package_from_a_direct_source_keeps_where_it_came_from(tmp_path):
    env = tmp_path / ".venv"
    site = env / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr/bin\n")
    sources = {
        "gitpkg": {"url": "https://example.com/r.git", "vcs_info": {"vcs": "git", "commit_id": "abc123"}},
        "localpkg": {"url": "file:///src/localpkg", "dir_info": {}},
        "myproject": {"url": "file:///ws", "dir_info": {"editable": True}},
        "fromindex": None,
    }
    for name, source in sources.items():
        info = site / f"{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
        if source is not None:
            (info / "direct_url.json").write_text(json.dumps(source))

    assert list_packages(env) == {
        "gitpkg": "1.0 @ git+https://example.com/r.git@abc123",
        "localpkg": "1.0 @ file:///src/localpkg",
        "myproject": "1.0 @ editable file:///ws",
        "fromindex": "1.0",
    }, "reinstalling by name alone could fetch an unrelated package of the same name from an index"


def test_an_npm_package_not_from_a_registry_keeps_where_it_came_from(tmp_path):
    web = tmp_path / "web"
    modules = web / "node_modules"
    (web / "package.json").parent.mkdir(parents=True)
    (web / "package.json").write_text('{"name": "web"}')
    for name, version in (("left-pad", "1.1.0"), ("gitpkg", "1.0.0"), ("localpkg", "0.1.0"), ("@scope/util", "2.0.0")):
        (modules / name).mkdir(parents=True)
        (modules / name / "package.json").write_text(json.dumps({"name": name, "version": version}))
    (modules / ".package-lock.json").write_text(json.dumps({"packages": {
        "node_modules/left-pad": {"version": "1.1.0",
                                  "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.1.0.tgz"},
        "node_modules/gitpkg": {"version": "1.0.0", "resolved": "git+ssh://git@example.com/gitpkg.git#abc123"},
        "node_modules/localpkg": {"resolved": "../localpkg", "link": True},
        "node_modules/@scope/util": {"version": "2.0.0",
                                     "resolved": "https://registry.npmjs.org/@scope/util/-/util-2.0.0.tgz"},
    }}))

    assert list_packages(modules) == {
        "left-pad": "1.1.0",
        "gitpkg": "1.0.0 @ git+ssh://git@example.com/gitpkg.git#abc123",
        "localpkg": "0.1.0 @ ../localpkg",
        "@scope/util": "2.0.0",
    }
