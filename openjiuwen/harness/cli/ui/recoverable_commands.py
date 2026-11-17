"""REPL commands for rollback, on with ``openjiuwen chat --recoverable``.

- ``/checkpoints [all]``: this session's checkpoints, newest first.
- ``/rollback <n>``: show what returning to checkpoint ``n`` changes, let the
  user strike undos, ask for confirmation, then apply. The next message
  continues from there.
- ``/resume <session_id>``: continue an earlier session; its context is
  restored from the stored history at the next message.
- ``/prune <keep>``: report what keeping only the latest ``keep`` checkpoints
  of each session frees, then delete on confirmation.

The backend's ``recoverable`` attribute is the ``RecoverableHarness``; without
it every command explains how to turn rollback on.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from openjiuwen.harness.recoverable import (
    EffectAction,
    EffectOutcome,
    RollbackApproval,
    RollbackStatus,
)

_OFF = "Rollback is off. Start the CLI with `openjiuwen chat --recoverable` to use it."
_SHOWN = 10
_ACTION_LABELS = {
    EffectAction.UNDO: "undo",
    EffectAction.REVIEW: "needs your OK",
    EffectAction.IRREVERSIBLE: "cannot undo",
    EffectAction.CANNOT_UNDO: "cannot undo",
    EffectAction.UNKNOWN: "outcome unknown",
    EffectAction.RETAINED: "kept",
}


async def _harness(backend: Any, console: Console) -> Any:
    """The backend's harness, bound to the working directory; ``None`` (and a hint) when rollback is off."""
    harness = getattr(backend, "recoverable", None)
    if harness is None:
        console.print(f"[yellow]{_OFF}[/yellow]")
        return None
    agent = getattr(backend, "agent", None)
    if agent is not None:
        await agent.ensure_initialized()  # Binds the working directory before the first message.
    return harness


async def _ask(prompt_session: Any, question: str) -> str:
    if prompt_session is None:
        return ""
    return (await prompt_session.prompt_async(question)).strip()


def _when(timestamp: float) -> str:
    local = time.localtime(timestamp)
    today = time.localtime()
    same_day = (local.tm_year, local.tm_yday) == (today.tm_year, today.tm_yday)
    return time.strftime("%H:%M:%S" if same_day else "%Y-%m-%d %H:%M", local)


def _size(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count} B"


def _listed(items: list[str]) -> str:
    shown = ", ".join(items[:_SHOWN])
    return shown + (f" and {len(items) - _SHOWN} more" if len(items) > _SHOWN else "")


def _numbers(answer: str) -> set[int]:
    return {int(part) for part in answer.replace(",", " ").split() if part.isdigit()}


# --------------------------------------------------------------- /checkpoints


async def cmd_checkpoints(console: Console, backend: Any = None, text: str = "", **_: Any) -> None:
    """List this session's checkpoints, newest first."""
    harness = await _harness(backend, console)
    if harness is None:
        return
    every = text.split()[1:] == ["all"]
    limit = None if every else 20
    history = await harness.history(backend.session_id, limit)
    if not history:
        console.print("[dim]No checkpoints yet: one is taken at each step once you send a message.[/dim]")
        return
    table = Table(title=f"Checkpoints of {backend.session_id}", show_lines=False)
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Time")
    table.add_column("What")
    for index, checkpoint in enumerate(history):
        label = escape(checkpoint.label or "")
        if index == 0:
            label += "  [dim](now)[/dim]"
        table.add_row(str(checkpoint.seq), _when(checkpoint.created_at), label)
    console.print(table)
    if limit is not None and len(history) == limit:
        console.print("[dim]Showing the latest 20; `/checkpoints all` lists every one.[/dim]")
    console.print("[dim]`/rollback <#>` returns to one.[/dim]")


# ------------------------------------------------------------------ /rollback


def _effect_line(number: Optional[int], effect: Any) -> str:
    label = _ACTION_LABELS.get(effect.action, effect.action.value)
    head = f"{number:>3}. " if number is not None else "     "
    line = f"{head}[{label}] {effect.tool_name}: {effect.args_summary[:80]}"
    command = (effect.undo_args or {}).get("command") if effect.undo_tool == "bash" else None
    if command:
        line += f"  -> {command[:80]}"
    if effect.detail:
        line += f"  ({effect.detail[:100]})"
    return escape(line)


def _show_plan(console: Console, plan: Any, target: Any) -> dict[int, Any]:
    """Print a plan; return the numbered effects a person can strike or confirm."""
    console.print(f"[bold]Rollback to #{target.seq}[/bold] {escape(target.label or '')} ({_when(target.created_at)})")
    console.print(f"  Discards {len(plan.abandoned_ids)} later checkpoint(s) and everything said and done in them.")
    if plan.workspace_restored:
        console.print(f"  Files: {len(plan.files_to_write)} written back, {len(plan.files_to_delete)} deleted.")
        for title, items in (("Written back", plan.files_to_write), ("Deleted", plan.files_to_delete),
                             ("Cannot be restored", plan.unrecoverable_files),
                             ("Build output deleted (rebuild later)", plan.build_outputs_to_delete)):
            if items:
                console.print(f"    {title}: {escape(_listed(items))}")
        if plan.lost_commits:
            console.print(f"  [yellow]Git: {plan.lost_commits} commit(s) on the current branches "
                          "will disappear.[/yellow]")
    else:
        console.print("  [yellow]Files are not rolled back (no snapshot of this checkpoint).[/yellow]")
    for change in plan.package_changes:
        parts = [f"remove {n}" for n in change.remove] + [f"install {n}=={v}" for n, v in change.install.items()]
        console.print(escape(f"  Dependencies in {change.env}: {', '.join(parts)}"
                             + (f" ({change.error})" if change.error else "")))
    if plan.processes_to_stop:
        console.print(f"  Background processes stopped: {', '.join(map(str, plan.processes_to_stop))}")
    for call in plan.cancelled_calls:
        line = f"  Cancels the call waiting for confirmation: {call.tool_name}({call.args_summary[:80]})"
        console.print(escape(line))
    numbered: dict[int, Any] = {}
    if plan.effects:
        console.print("  Outside the working directory, newest first:")
        for effect in plan.effects:
            number = None
            if effect.action in (EffectAction.UNDO, EffectAction.REVIEW):
                number = len(numbered) + 1
                numbered[number] = effect
            console.print(_effect_line(number, effect))
    console.print("  [yellow]! A rollback cannot be undone: later changes, yours included, are lost.[/yellow]")
    console.print("  [yellow]! Save or close files open in an editor first, "
                  "or saving them undoes the rollback.[/yellow]")
    console.print("  [yellow]! Effects outside the directory are known only from what the agent recorded.[/yellow]")
    return numbered


async def _choose(prompt_session: Any, numbered: dict[int, Any]) -> tuple[list[str], list[str]]:
    """Undos the person strikes, and failed calls' undos they confirm."""
    cancelled, confirmed = [], []
    undos = {n: e for n, e in numbered.items() if e.action == EffectAction.UNDO}
    reviews = {n: e for n, e in numbered.items() if e.action == EffectAction.REVIEW}
    if undos:
        answer = await _ask(prompt_session, "Skip any undo? Numbers (e.g. 1,3), or Enter for none: ")
        cancelled = [undos[n].record_id for n in _numbers(answer) if n in undos]
    if reviews:
        answer = await _ask(prompt_session, "Run any undo marked 'needs your OK'? Numbers, or Enter for none: ")
        confirmed = [reviews[n].record_id for n in _numbers(answer) if n in reviews]
    return cancelled, confirmed


def _show_result(console: Console, result: Any) -> None:
    style = "green" if result.status == RollbackStatus.COMPLETED else "yellow"
    console.print(f"[{style}]Rollback {result.status.value}.[/{style}]", end=" ")
    if result.workspace_restored:
        console.print(f"{len(result.files_written)} file(s) written back, {len(result.files_deleted)} deleted.")
    else:
        console.print()
    groups = (
        ("Undone", EffectOutcome.UNDONE), ("Left in place as you asked", EffectOutcome.CANCELLED),
        ("Left as they are: changed again after the agent", EffectOutcome.KEPT),
        ("Undo FAILED", EffectOutcome.FAILED), ("Not attempted", EffectOutcome.PENDING),
    )
    for title, outcome in groups:
        effects = [e for e in result.effects if e.outcome == outcome]
        if effects:
            console.print(escape(f"  {title}: " + "; ".join(
                f"{e.tool_name}: {e.args_summary[:60]}" + (f" ({e.detail[:80]})" if e.detail else "")
                for e in effects
            )))
    if result.unrestored_files:
        console.print(escape(f"  Files NOT restored: {_listed(result.unrestored_files)}"))
    for change in result.package_changes:
        if change.error:
            console.print(escape(f"  Dependencies in {change.env} NOT restored: {change.error}"))
    if result.processes_stopped:
        console.print(f"  Background processes stopped: {', '.join(map(str, result.processes_stopped))}")


async def cmd_rollback(
    console: Console, backend: Any = None, text: str = "", prompt_session: Any = None, **_: Any,
) -> None:
    """Return the working directory and the conversation to a checkpoint, after confirmation."""
    harness = await _harness(backend, console)
    if harness is None:
        return
    args = text.split()[1:]
    if len(args) != 1 or not args[0].lstrip("#").isdigit():
        console.print("Usage: /rollback <#>   (see /checkpoints)")
        return
    seq = int(args[0].lstrip("#"))
    target = next((c for c in await harness.history(backend.session_id) if c.seq == seq), None)
    if target is None:
        console.print(f"[red]No checkpoint #{seq} in this session; see /checkpoints.[/red]")
        return
    plan = await harness.plan_rollback(backend.session_id, target.checkpoint_id, reason="requested in the CLI")
    numbered = _show_plan(console, plan, target)
    cancelled, confirmed = await _choose(prompt_session, numbered)
    if (await _ask(prompt_session, "Apply this rollback? [y/N] ")).lower() not in ("y", "yes"):
        await harness.decline_rollback(plan.plan_id, "not confirmed in the CLI")
        console.print("[dim]Rollback cancelled; nothing changed.[/dim]")
        return
    result = await harness.apply_rollback(RollbackApproval(
        plan_id=plan.plan_id, target_id=plan.target_id,
        cancelled_effect_ids=cancelled, confirmed_effect_ids=confirmed,
    ))
    _show_result(console, result)
    while result.status == RollbackStatus.PARTIAL:
        answer = (await _ask(prompt_session, "Some items are unfinished: [r]etry now, [a]ccept as is, or Enter "
                                             "to decide later: ")).lower()
        if answer.startswith("r"):
            result = await harness.resume_rollback(plan.plan_id)
            _show_result(console, result)
        elif answer.startswith("a"):
            await harness.accept_partial(plan.plan_id)
            console.print("[dim]Accepted: what was not undone stays in effect.[/dim]")
            break
        else:
            break
    console.print(f"[dim]Your next message continues from checkpoint #{seq}.[/dim]")


# -------------------------------------------------------------------- /resume


async def resume_note(backend: Any, session_id: str) -> str:
    """One line on where a resumed session stands; it names no checkpoint when none is stored."""
    history = await backend.recoverable.history(session_id, 1)
    if not history:
        return f"No stored history for session {session_id}: it starts empty."
    head = history[0]
    return f"Resumed session {session_id} at checkpoint #{head.seq} ({head.label}). Your next message continues it."


async def cmd_resume(
    console: Console, backend: Any = None, store: Any = None, text: str = "", cfg: Any = None, **_: Any,
) -> None:
    """Continue an earlier session in this REPL."""
    harness = await _harness(backend, console)
    if harness is None:
        return
    args = text.split()[1:]
    if len(args) != 1:
        console.print("Usage: /resume <session_id>   (see /sessions)")
        return
    session_id = args[0]
    if not await harness.history(session_id, 1):
        console.print(f"[red]No stored history for session {escape(session_id)}; see /sessions.[/red]")
        return
    backend.use_session(session_id)
    if store is not None:
        store.resume_session(session_id, getattr(cfg, "model", ""))
    console.print(escape(await resume_note(backend, session_id)))


# --------------------------------------------------------------------- /prune


async def cmd_prune(console: Console, backend: Any = None, text: str = "", prompt_session: Any = None,
                    **_: Any) -> None:
    """Free rollback storage: keep the latest checkpoints of each session, after showing what that frees."""
    harness = await _harness(backend, console)
    if harness is None:
        return
    args = text.split()[1:]
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) < 1:
        status = await harness.storage_status()
        console.print(f"Rollback storage: {_size(status.stored_bytes)} stored, "
                      f"{_size(status.disk_free)} free on the disk.")
        console.print("Usage: /prune <keep>   keeps the latest <keep> checkpoints of each session")
        return
    keep = int(args[0])
    preview = await harness.prune_checkpoints(keep, dry_run=True)
    count = sum(preview.checkpoints.values())
    if not count:
        console.print(f"[dim]Every session has at most {keep} checkpoint(s); nothing to delete.[/dim]")
        return
    console.print(f"Keeping the latest {keep} checkpoint(s) of each session deletes {count} checkpoint(s) in "
                  f"{len(preview.checkpoints)} session(s) and frees {_size(preview.freed_bytes)}. "
                  "Deleted checkpoints can no longer be rollback targets.")
    if (await _ask(prompt_session, "Delete them? [y/N] ")).lower() not in ("y", "yes"):
        console.print("[dim]Nothing deleted.[/dim]")
        return
    done = await harness.prune_checkpoints(keep)
    console.print(f"Deleted {sum(done.checkpoints.values())} checkpoint(s), freed {_size(done.freed_bytes)}.")


def print_notices(console: Console, backend: Any) -> None:
    """Print, once, the messages the backend collected outside a turn."""
    notices = getattr(backend, "notices", None)
    while notices:
        console.print(f"[yellow]{escape(notices.pop(0))}[/yellow]")


COMMANDS = {
    "/checkpoints": (cmd_checkpoints, "List checkpoints (rollback targets)"),
    "/rollback": (cmd_rollback, "Return to a checkpoint: /rollback <#>"),
    "/resume": (cmd_resume, "Continue an earlier session: /resume <id>"),
    "/prune": (cmd_prune, "Free rollback storage: /prune <keep>"),
}
"""Command name to handler and help text; the REPL registers them."""
