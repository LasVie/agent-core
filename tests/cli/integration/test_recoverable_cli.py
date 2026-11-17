"""Rollback in the CLI: ``chat --recoverable`` and the /checkpoints, /rollback, /resume, /prune commands.

The backend is the CLI's ``LocalBackend``; its agent is a small DeepAgent whose tool
writes project files directly, so the CLI's write confirmations and the user's
MCP configuration stay out of the way. Rollback storage lives in the test's
temporary directory, not under the home directory.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import pytest_asyncio
from rich.console import Console

from openjiuwen.core.runner import Runner
from openjiuwen.harness.cli.agent.config import CLIConfig
from openjiuwen.harness.cli.agent.factory import LocalBackend
from openjiuwen.harness.cli.cli import CLIOptions, _run_chat
from openjiuwen.harness.cli.storage.session_store import SessionStore
from openjiuwen.harness.cli.ui.recoverable_commands import (
    cmd_checkpoints,
    cmd_prune,
    cmd_resume,
    cmd_rollback,
    print_notices,
)
from openjiuwen.harness.recoverable import RecoverableHarness
from openjiuwen.harness.recoverable import harness as harness_module
from tests.unit_tests.fixtures.deep_agent_interrupts import make_deep_agent, messages_seen, run, scripted
from tests.unit_tests.fixtures.mock_llm import MockLLMModel, create_text_response, create_tool_call_response
from tests.unit_tests.harness.recoverable.test_ledger import DECLARATIONS, CloseTicket, CreateTicket, TicketService
from tests.unit_tests.harness.recoverable.test_workspace import FileTool


class Answers:
    """Stands in for the REPL's prompt session: answers questions from a script."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.asked: list[str] = []

    async def prompt_async(self, question: str) -> str:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else ""


@pytest_asyncio.fixture
async def runner():
    await Runner.start()
    try:
        yield
    finally:
        await Runner.stop()


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """The directory the user runs the CLI in; rollback storage goes next to it."""
    monkeypatch.setattr(harness_module, "DEFAULT_STORAGE_DIR", tmp_path / "recoverable")
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("v0")
    return project


def _backend(project: Path, *, recoverable: bool = True, session_id: str = "cli-test", tools=(),
             declarations=None) -> LocalBackend:
    cfg = CLIConfig(api_key="mock-api-key", cwd=str(project), workspace=str(project.parent / "agent-state"),
                    recoverable=recoverable)
    backend = LocalBackend(cfg)
    if declarations is not None:  # The CLI declares no host tools; the tests' ticket service needs some.
        backend.recoverable = RecoverableHarness(workspace_root=project, declarations=declarations,
                                                 on_event=backend._on_event)
    backend.use_session(session_id)
    rails = [backend.recoverable.rail()] if backend.recoverable is not None else []
    backend.agent = make_deep_agent(project.parent / "agent-state", tools=[FileTool(project), *tools], rails=rails,
                                    name="RecoverableCli")
    return backend


def _console() -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    return Console(file=out, width=200), out


def _edit(call_id: str, content: str):
    return create_tool_call_response("edit", json.dumps({"path": "app.py", "content": content}), tool_call_id=call_id)


def _model(*responses) -> MockLLMModel:
    llm = MockLLMModel()
    llm.set_responses(list(responses))
    return llm


@pytest.mark.asyncio
async def test_the_harness_snapshots_the_working_directory_not_the_agent_state(project):
    backend = _backend(project)
    await backend.agent.ensure_initialized()

    assert backend.recoverable.workspace_root == project.resolve()
    assert _backend(project, recoverable=False).recoverable is None, "off unless asked for"


@pytest.mark.asyncio
async def test_checkpoints_are_listed_and_a_confirmed_rollback_restores_files_and_context(project, runner):
    backend = _backend(project)
    llm = _model(_edit("c1", "v1"), _edit("c2", "v2"), create_text_response("done"),
                 create_text_response("again"))
    console, out = _console()
    with scripted(llm):
        await run(backend.agent, "change app.py twice", backend.session_id)
        await cmd_checkpoints(console, backend=backend, text="/checkpoints")
        listing = out.getvalue()
        assert "user: change app.py twice" in listing and "edit" in listing and "reply: done" in listing

        prompts = Answers("y")
        await cmd_rollback(console, backend=backend, text="/rollback 1", prompt_session=prompts)
        assert prompts.asked == ["Apply this rollback? [y/N] "], "nothing to strike: only the confirmation"
        assert (project / "app.py").read_text() == "v1"
        assert "Rollback completed" in out.getvalue() and "continues from checkpoint #1" in out.getvalue()

        await run(backend.agent, "go on", backend.session_id)
    seen = [str(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in messages_seen(llm)]
    assert any(text.startswith("[ROLLBACK]") for text in seen)


@pytest.mark.asyncio
async def test_an_unconfirmed_rollback_changes_nothing(project, runner):
    backend = _backend(project)
    console, out = _console()
    with scripted(_model(_edit("c1", "v1"), create_text_response("done"))):
        await run(backend.agent, "change app.py", backend.session_id)
    before = len(await backend.recoverable.history(backend.session_id))

    await cmd_rollback(console, backend=backend, text="/rollback 0", prompt_session=Answers("n"))

    assert "nothing changed" in out.getvalue()
    assert (project / "app.py").read_text() == "v1"
    assert len(await backend.recoverable.history(backend.session_id)) == before
    await cmd_rollback(console, backend=backend, text="/rollback 99", prompt_session=Answers())
    assert "No checkpoint #99" in out.getvalue()


@pytest.mark.asyncio
async def test_a_session_is_resumed_after_a_restart(project, runner):
    first = _backend(project, session_id="cli-earlier")
    llm = _model(_edit("c1", "v1"), create_text_response("first done"), create_text_response("continued"))
    with scripted(llm):
        await run(first.agent, "remember the word apricot", first.session_id)
        first.recoverable.release_session(first.session_id)  # The old process exits.

        second = _backend(project)  # A new CLI process, started on a fresh session.
        store = SessionStore(project.parent / "sessions")
        console, out = _console()
        await cmd_resume(console, backend=second, store=store, text="/resume cli-earlier",
                         cfg=second.cfg)
        assert second.session_id == "cli-earlier"
        assert "Resumed session cli-earlier at checkpoint" in out.getvalue()

        await run(second.agent, "what was the word?", second.session_id)
    seen = [str(m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) for m in messages_seen(llm)]
    assert any("apricot" in text for text in seen), "the earlier context came back"

    await cmd_resume(console, backend=second, store=store, text="/resume no-such-session", cfg=second.cfg)
    assert "No stored history for session no-such-session" in out.getvalue()


@pytest.mark.asyncio
async def test_prune_reports_first_and_deletes_on_confirmation(project, runner):
    backend = _backend(project)
    with scripted(_model(_edit("c1", "v1" * 1000), _edit("c2", "v2"), create_text_response("done"))):
        await run(backend.agent, "change app.py", backend.session_id)
    console, out = _console()

    await cmd_prune(console, backend=backend, text="/prune 2", prompt_session=Answers("n"))
    assert "deletes 2 checkpoint(s)" in out.getvalue() and "Nothing deleted" in out.getvalue()
    assert len(await backend.recoverable.history(backend.session_id)) == 4

    await cmd_prune(console, backend=backend, text="/prune 2", prompt_session=Answers("y"))
    assert "Deleted 2 checkpoint(s)" in out.getvalue()
    assert [c.seq for c in await backend.recoverable.history(backend.session_id)] == [3, 2]


@pytest.mark.asyncio
async def test_the_commands_explain_how_to_turn_rollback_on(project):
    backend = _backend(project, recoverable=False)
    console, out = _console()
    for command in (cmd_checkpoints, cmd_rollback, cmd_resume, cmd_prune):
        await command(console, backend=backend, text="/x 1")
    assert out.getvalue().count("openjiuwen chat --recoverable") == 4


@pytest.mark.asyncio
async def test_notices_are_printed_once(project):
    backend = _backend(project)
    backend.notices.append("Rollback storage: less than 20% of the disk is free.")
    console, out = _console()
    print_notices(console, backend)
    print_notices(console, backend)
    assert out.getvalue().count("less than 20%") == 1


@pytest.mark.asyncio
async def test_storage_inside_the_working_directory_is_refused_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_module, "DEFAULT_STORAGE_DIR", tmp_path / "project" / ".rollback")
    (tmp_path / "project").mkdir()
    backend = LocalBackend(CLIConfig(api_key="mock-api-key", cwd=str(tmp_path / "project"), recoverable=True))
    with pytest.raises(ValueError, match="--recoverable cannot be used in"):
        await backend.start()


@pytest.mark.asyncio
async def test_resume_needs_recoverable():
    with pytest.raises(ValueError, match="--resume needs --recoverable"):
        await _run_chat(CLIOptions(api_key="mock-api-key", resume="cli-earlier"))


@pytest.mark.asyncio
async def test_a_person_strikes_an_undo_before_confirming(project, runner):
    service = TicketService()
    backend = _backend(project, tools=[CreateTicket(service), CloseTicket(service)], declarations=DECLARATIONS)

    def create(call_id: str, title: str):
        return create_tool_call_response("create_ticket", json.dumps({"title": title}), tool_call_id=call_id)

    with scripted(_model(create("c1", "one"), create("c2", "two"), create_text_response("done"))):
        await run(backend.agent, "open two tickets", backend.session_id)
    console, out = _console()

    prompts = Answers("2", "y")  # Effects are listed newest first: 1 closes T-2, 2 closes T-1.
    await cmd_rollback(console, backend=backend, text="/rollback 0", prompt_session=prompts)

    assert prompts.asked[0].startswith("Skip any undo?")
    assert "1. [undo] create_ticket" in out.getvalue() and "2. [undo] create_ticket" in out.getvalue()
    assert service.closed == ["T-2"] and "T-1" in service.open
    assert "Left in place as you asked" in out.getvalue()


@pytest.mark.asyncio
async def test_switching_sessions_in_one_repl_lets_the_new_one_work(project, runner):
    backend = _backend(project, session_id="cli-one")
    console, out = _console()
    with scripted(_model(*[create_text_response(f"answer {i}") for i in range(3)])):
        await run(backend.agent, "first session", backend.session_id)
        backend.use_session("cli-two")
        await run(backend.agent, "second session", backend.session_id)  # cli-one held the directory until now.
        await cmd_resume(console, backend=backend, store=None, text="/resume cli-one", cfg=backend.cfg)
        await run(backend.agent, "back to the first", backend.session_id)

    assert backend.session_id == "cli-one"
    assert [c.label for c in await backend.recoverable.history("cli-one", 1)] == ["reply: answer 2"]


def test_chat_takes_recoverable_and_resume(monkeypatch):
    from click.testing import CliRunner

    from openjiuwen.harness.cli import cli as cli_module

    seen = {}

    async def fake_run_chat(opts):
        seen["opts"] = opts

    monkeypatch.setattr(cli_module, "_run_chat", fake_run_chat)
    result = CliRunner().invoke(cli_module.cli, ["chat", "--recoverable", "--resume", "cli-earlier"])

    assert result.exit_code == 0, result.output
    assert (seen["opts"].recoverable, seen["opts"].resume) == (True, "cli-earlier")


def test_rollback_can_be_turned_on_by_environment(monkeypatch):
    from openjiuwen.harness.cli.agent.config import load_config

    monkeypatch.setenv("OPENJIUWEN_API_KEY", "mock-api-key")
    assert load_config().recoverable is False
    monkeypatch.setenv("OPENJIUWEN_RECOVERABLE", "1")
    assert load_config().recoverable is True


def test_the_plan_names_a_call_waiting_for_confirmation_that_it_cancels():
    """The REPL answers confirmations within the turn; other hosts' sessions reach /rollback paused."""
    import time
    from types import SimpleNamespace

    from openjiuwen.harness.cli.ui.recoverable_commands import _show_plan
    from openjiuwen.harness.recoverable import PausedCall, RollbackPlan

    plan = RollbackPlan(plan_id="p", session_id="cli-test", target_id="c1", head_id="c1", abandoned_ids=[],
                        cancelled_calls=[PausedCall(tool_call_id="call_1", interrupt_id="i1", tool_name="deploy",
                                                    args_summary='{"env": "test"}')])
    console, out = _console()
    _show_plan(console, plan, SimpleNamespace(seq=1, label="build", created_at=time.time()))

    assert 'Cancels the call waiting for confirmation: deploy({"env": "test"})' in out.getvalue()
