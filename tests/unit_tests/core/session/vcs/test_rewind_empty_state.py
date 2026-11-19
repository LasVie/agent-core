# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Rewinding to a point whose state was empty empties the live state.

``set_state(None)`` means there is nothing to restore and keeps the state;
``set_state({})`` is a real empty state. Before, both were ignored, so a key
written after an empty snapshot survived a rewind.
"""
import pytest

from openjiuwen.core.context_engine.context_engine import ContextEngine
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.state.agent_state import StateCollection
from openjiuwen.core.session.state.base import AGENT_STATE_KEY, GLOBAL_STATE_KEY
from openjiuwen.core.session.vcs import VersioningConfig, for_session


def test_an_empty_state_replaces_and_none_keeps():
    state = StateCollection()
    state.update_global({"later": 1})
    state.update({"later": 2})

    state.set_state({})
    assert state.get_state() == {GLOBAL_STATE_KEY: {"later": 1}, AGENT_STATE_KEY: {"later": 2}}, (
        "a snapshot without these parts restores nothing"
    )

    state.set_state({GLOBAL_STATE_KEY: {}, AGENT_STATE_KEY: {}})
    assert state.get_state() == {GLOBAL_STATE_KEY: {}, AGENT_STATE_KEY: {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("before", [{}, {"keep": 1}])
async def test_rewind_drops_keys_written_after_the_target(before):
    engine = ContextEngine()
    session = create_agent_session(session_id="src")
    await engine.create_context("default_context_id", session)
    manager = for_session(session, engine, config=VersioningConfig(backend="kv"), kv_store=InMemoryKVStore())
    if before:
        session.update_state(before)
    await manager.append()
    session.update_state({"later": 1})
    await manager.append()

    await manager.rewind("e1")

    assert {key: value for key, value in session.get_state().items() if key != "context"} == before
