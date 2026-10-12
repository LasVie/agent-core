# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Third-party provider resolution: in-process registration and installed entry points."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, ModelSpec
from openjiuwen.harness.schema.extension_spec import AgentTemplateSpec, PromptSectionSpec
from openjiuwen.harness_protocol import HarnessCard, HarnessProvider
from openjiuwen.harness_providers import (
    HARNESS_PROVIDER_ENTRY_POINT_GROUP,
    available_providers,
    build_harness_context,
    create_harness,
    register_harness_provider,
    resolve_provider,
    unregister_harness_provider,
)
from openjiuwen.harness_providers import factory
from openjiuwen.harness_providers.skills import SkillSource, install_skills
from tests.test_logger import logger

_THIS_MODULE = "tests.unit_tests.harness_providers.test_provider_discovery"


class FakeHarness:
    """Stands in for a harness; records the configuration it was created with."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config


class FakeProvider:
    """A provider without a ``manifest_config`` hook."""

    @property
    def card(self) -> HarnessCard:
        return HarnessCard(name="fake-cli", implementation_version="0.1.0")

    @staticmethod
    def create(config: dict[str, Any]) -> FakeHarness:
        return FakeHarness(dict(config))


class EndpointAwareProvider(FakeProvider):
    """A provider mapping the manifest endpoint onto its own fields."""

    @staticmethod
    def manifest_config(endpoint: dict[str, str | None]) -> dict[str, Any]:
        return {"model_name": endpoint["model"], "base_url": endpoint["api_base"], "token": endpoint["api_key"]}


def _manifest(**overrides: Any) -> AgentTemplateSpec:
    values: dict[str, Any] = {
        "agent_card": AgentCard(id="expert", name="expert", description="An expert agent"),
        "model": ModelSpec(
            model_client_config=ModelClientConfig(client_provider="openai", api_key="secret", api_base="https://llm"),
            model_request_config=ModelRequestConfig(model="gpt-x"),
        ),
        "prompt_sections": [PromptSectionSpec(name="identity", content={"en": "You are an expert."})],
    }
    values.update(overrides)
    return AgentTemplateSpec(**values)


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> list[EntryPoint]:
    """Replace the installed entry points with a controllable list."""
    entries: list[EntryPoint] = []
    monkeypatch.setattr(factory, "_provider_entry_points", lambda: tuple(entries))
    return entries


def test_entry_point_group_is_the_published_name() -> None:
    assert HARNESS_PROVIDER_ENTRY_POINT_GROUP == "openjiuwen.harness_providers"


def test_installed_entry_point_resolves_and_creates_harnesses(installed: list[EntryPoint]) -> None:
    installed.append(EntryPoint("fakecli", f"{_THIS_MODULE}:FakeProvider", HARNESS_PROVIDER_ENTRY_POINT_GROUP))
    provider = resolve_provider("fakecli")
    assert isinstance(provider, HarnessProvider)
    assert provider.card.name == "fake-cli"
    assert "fakecli" in available_providers()
    harness = create_harness(_manifest(), provider="fakecli", config={"cwd": "/w"})
    logger.info("third-party config: %s", harness.config)
    assert isinstance(harness, FakeHarness)
    # Without a manifest_config hook only the model name is passed on.
    assert harness.config == {"cwd": "/w", "model": "gpt-x"}


def test_manifest_config_hook_maps_the_endpoint_and_explicit_config_wins(installed: list[EntryPoint]) -> None:
    installed.append(EntryPoint("aware", f"{_THIS_MODULE}:EndpointAwareProvider", HARNESS_PROVIDER_ENTRY_POINT_GROUP))
    harness = create_harness(_manifest(), provider="aware", config={"token": "override"})
    assert harness.config == {"token": "override", "model_name": "gpt-x", "base_url": "https://llm"}


def test_third_party_providers_reject_deepagent_only_sections(installed: list[EntryPoint]) -> None:
    installed.append(EntryPoint("fakecli", f"{_THIS_MODULE}:FakeProvider", HARNESS_PROVIDER_ENTRY_POINT_GROUP))
    with pytest.raises(ValueError, match="DeepAgent framework"):
        create_harness(_manifest(tools=[BuiltinToolSpec(type="core.web_search")]), provider="fakecli")


def test_third_party_context_renders_the_manifest_prompt(installed: list[EntryPoint]) -> None:
    installed.append(EntryPoint("fakecli", f"{_THIS_MODULE}:FakeProvider", HARNESS_PROVIDER_ENTRY_POINT_GROUP))
    context = build_harness_context(_manifest(), provider="fakecli", host_session_id="h", language="en")
    assert "You are an expert." in context.system_prompt


def test_broken_or_non_provider_entry_points_fail_loudly(installed: list[EntryPoint]) -> None:
    installed.append(EntryPoint("broken", "no_such_module_for_tests:Provider", HARNESS_PROVIDER_ENTRY_POINT_GROUP))
    installed.append(EntryPoint("notprovider", f"{_THIS_MODULE}:FakeHarness", HARNESS_PROVIDER_ENTRY_POINT_GROUP))
    with pytest.raises(ValueError, match="cannot be loaded"):
        resolve_provider("broken")
    with pytest.raises(ValueError, match="could not be instantiated"):
        resolve_provider("notprovider")
    with pytest.raises(ValueError, match="unknown harness provider"):
        resolve_provider("missing")


def test_builtin_names_win_over_installed_entry_points(installed: list[EntryPoint]) -> None:
    installed.append(EntryPoint("codex", f"{_THIS_MODULE}:FakeProvider", HARNESS_PROVIDER_ENTRY_POINT_GROUP))
    assert resolve_provider("codex").card.name == "codex"
    assert available_providers().count("codex") == 1


def test_in_process_registration(installed: list[EntryPoint]) -> None:
    _ = installed
    with pytest.raises(ValueError):
        register_harness_provider("claudecode", FakeProvider)
    register_harness_provider("private", FakeProvider)
    try:
        assert resolve_provider("private").card.name == "fake-cli"
        assert "private" in available_providers()
    finally:
        unregister_harness_provider("private")
    with pytest.raises(ValueError):
        resolve_provider("private")


def test_install_skills_accepts_a_third_party_scan_dir(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: example\ndescription: Test skill\n---\nbody\n")
    project = tmp_path / "project"
    project.mkdir()
    installed = install_skills((SkillSource(str(source)),), provider="fake-cli", cwd=str(project), scan_dir=".fake/skills")
    assert installed == (project / ".fake" / "skills" / "example",)
    with pytest.raises(ValueError, match="no skill discovery directory"):
        install_skills((SkillSource(str(source)),), provider="fake-cli", cwd=str(project))
    with pytest.raises(ValueError, match="escapes"):
        install_skills((SkillSource(str(source)),), provider="fake-cli", cwd=str(project), scan_dir="../outside")
