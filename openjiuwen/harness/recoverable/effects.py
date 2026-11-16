# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Side-effect declarations: what a tool does outside the workspace, and how to undo it.

Workspace files and dependency directories are not effects here; the workspace
snapshot restores them. A declaration answers one question per tool: does it
change anything else, and if so, can that change be undone?

Tools whose effect is the same on every call are declared once in a table, keyed
by tool name. ``BUILTIN_DECLARATIONS`` covers the harness's own tools; a host adds
or overrides entries for its tools. A tool missing from the table is irreversible.

Shell and code execution do something different on every call, so the model
declares each call through three extra arguments (``side_effect``,
``reverse_command``, ``external_paths``). An undeclared call is irreversible, and a
shell command on the outward list counts as irreversible whatever it declares.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error


class EffectCategory(str, Enum):
    NONE = "none"
    """No effect outside the workspace: reads, file edits, plans."""
    REVERSIBLE = "reversible"
    """An effect that a declared undo tool reverses."""
    IRREVERSIBLE = "irreversible"
    """An effect that cannot be undone, such as a sent message."""
    RETAINED = "retained"
    """Kept on purpose on rollback, such as memory writes."""


class EffectDeclaration(BaseModel):
    """How one tool affects the world outside the workspace.

    For a reversible tool, ``undo_args`` maps each argument of ``undo_tool`` to a
    path in the original call: ``"result.<key>[.<key>...]"`` reads the tool result,
    ``"args.<key>[.<key>...]"`` reads the call arguments. ``undo_constants`` adds
    fixed arguments.
    """

    category: EffectCategory
    undo_tool: str | None = None
    undo_args: dict[str, str] = Field(default_factory=dict)
    undo_constants: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_undo(self) -> "EffectDeclaration":
        if self.category == EffectCategory.REVERSIBLE and not self.undo_tool:
            raise build_error(StatusCode.DEEPAGENT_CONFIG_PARAM_ERROR, error_msg="a reversible tool needs an undo_tool")
        for source in self.undo_args.values():
            if not source.startswith(("result.", "args.")):
                raise build_error(
                    StatusCode.DEEPAGENT_CONFIG_PARAM_ERROR,
                    error_msg=f"undo argument source must start with 'result.' or 'args.': {source}",
                )
        return self


_NONE = EffectDeclaration(category=EffectCategory.NONE)
_RETAINED = EffectDeclaration(category=EffectCategory.RETAINED)
IRREVERSIBLE = EffectDeclaration(category=EffectCategory.IRREVERSIBLE)

BUILTIN_DECLARATIONS: dict[str, EffectDeclaration] = {
    # Files in the workspace: the snapshot restores them.
    **{name: _NONE for name in ("read_file", "write_file", "edit_file", "glob", "list_files", "grep")},
    # Plans and modes live in the session state, which vcs restores.
    **{name: _NONE for name in ("todo_create", "todo_list", "todo_get", "todo_modify",
                                "enter_plan_mode", "exit_plan_mode", "switch_mode")},
    # Reads, searches and questions.
    **{name: _NONE for name in (
        "ask_user", "fetch_webpage", "free_search", "paid_search", "tool_search", "recall_compressed_context",
        "memory_search", "memory_get", "read_memory", "coding_memory_read", "get_current_goal",
        "audio_metadata", "audio_question_answering", "audio_transcription", "image_ocr",
        "video_understanding", "visual_question_answering",
        "cron_get_job", "cron_list_jobs", "cron_preview_job", "subagent_list",
    )},
    # Memory is kept across rollbacks (design A4); its directories are outside the snapshot.
    **{name: _RETAINED for name in ("write_memory", "edit_memory", "coding_memory_write", "coding_memory_edit")},
    "cron_create_job": EffectDeclaration(
        category=EffectCategory.REVERSIBLE, undo_tool="cron_delete_job", undo_args={"job_id": "result.id"},
    ),
}
"""Built-in harness tools. Shell and code execution are declared per call, see below."""

SHELL_TOOL = "bash"
PER_CALL_TOOLS = frozenset({SHELL_TOOL, "code"})
"""Tools the model declares call by call. A reverse command always runs through the shell."""

SIDE_EFFECT_ARG = "side_effect"
REVERSE_COMMAND_ARG = "reverse_command"
EXTERNAL_PATHS_ARG = "external_paths"

DEFAULT_OUTWARD_COMMANDS = frozenset({
    "ssh", "scp", "sftp", "kubectl", "helm", "docker", "podman", "aws", "gcloud", "az", "terraform",
    "psql", "mysql", "mongosh", "redis-cli", "crontab", "systemctl", "launchctl",
})
"""Programs whose effect is outside the machine or the workspace. ``git push`` and
writing ``curl`` / ``wget`` requests are checked separately."""

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CURL_DATA_FLAGS = frozenset({"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form",
                              "-T", "--upload-file", "--json"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n"})


@dataclass(frozen=True)
class CallDeclaration:
    """The declaration that applies to one call, with why it was chosen."""

    declaration: EffectDeclaration
    detail: str = ""


def _argv_lists(command: str) -> list[list[str]]:
    """Each simple command of ``command`` as an argv, best effort."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    commands, current = [], []
    for token in tokens:
        if token in _SEPARATORS or set(token) <= set(";&|()\n"):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _program(argv: list[str]) -> tuple[str, list[str]]:
    """Skip environment assignments and ``sudo`` / ``env``; return the program and its arguments."""
    index = 0
    while index < len(argv) and ("=" in argv[index] and not argv[index].startswith("-")
                                 or argv[index] in ("sudo", "env", "command", "exec", "nohup", "time")):
        index += 1
    if index >= len(argv):
        return "", []
    return os.path.basename(argv[index]), argv[index + 1:]


def outward_reason(command: str, outward: frozenset[str] = DEFAULT_OUTWARD_COMMANDS) -> str | None:
    """Why ``command`` affects something beyond the workspace, or ``None``."""
    for argv in _argv_lists(command):
        program, args = _program(argv)
        if program in outward:
            return f"`{program}` acts outside the workspace"
        if program == "git" and "push" in args:
            return "`git push` publishes commits"
        if program in ("curl", "wget"):
            methods = {args[i + 1].upper() for i, a in enumerate(args[:-1]) if a in ("-X", "--request", "--method")}
            methods |= {a.split("=", 1)[1].upper() for a in args if a.startswith(("--request=", "--method="))}
            if methods & _WRITE_METHODS or any(a in _CURL_DATA_FLAGS or a.startswith("--data") for a in args):
                return f"`{program}` sends a request that writes"
    return None


class DeclarationTable:
    """Looks up the declaration of a tool, host entries first."""

    def __init__(
        self,
        overrides: dict[str, EffectDeclaration] | None = None,
        outward_commands: frozenset[str] = DEFAULT_OUTWARD_COMMANDS,
    ):
        self._overrides = dict(overrides or {})
        self._table = {**BUILTIN_DECLARATIONS, **self._overrides}
        self._outward = outward_commands

    def get(self, tool_name: str) -> EffectDeclaration:
        return self._table.get(tool_name, IRREVERSIBLE)

    def declared_per_call(self, tool_name: str) -> bool:
        """True when the model declares each call of ``tool_name``; a host entry turns that off."""
        return tool_name in PER_CALL_TOOLS and tool_name not in self._overrides

    def for_call(self, tool_name: str, args: dict[str, Any]) -> CallDeclaration:
        """The declaration of one call: the table, or for shell and code the call's own arguments."""
        if not self.declared_per_call(tool_name):
            return CallDeclaration(self.get(tool_name))
        declared = args.get(SIDE_EFFECT_ARG)
        if declared == EffectCategory.NONE.value:
            reason = outward_reason(str(args.get("command") or ""), self._outward) if tool_name == SHELL_TOOL else None
            if reason:
                return CallDeclaration(IRREVERSIBLE, f"declared without effect, but {reason}")
            return CallDeclaration(EffectDeclaration(category=EffectCategory.NONE))
        if declared == EffectCategory.REVERSIBLE.value:
            return CallDeclaration(EffectDeclaration(
                category=EffectCategory.REVERSIBLE, undo_tool=SHELL_TOOL,
                undo_args={"command": f"args.{REVERSE_COMMAND_ARG}"},
            ))
        if declared == EffectCategory.IRREVERSIBLE.value:
            return CallDeclaration(IRREVERSIBLE)
        return CallDeclaration(IRREVERSIBLE, "no side_effect was declared")


def declaration_schema(language: str) -> dict[str, dict[str, Any]]:
    """JSON-schema properties the model fills to declare a shell or code call."""
    if language == "en":
        texts = (
            "What this call changes OUTSIDE the workspace. Workspace files and installed dependencies are "
            "restored by rollback, so changing only them is 'none'. 'reversible': it only CREATES something "
            "outside (a container, a cloud resource, a scheduled job) and reverse_command removes it. "
            "'irreversible': it sends, publishes, deploys, or overwrites or deletes anything outside.",
            "For 'reversible' only: the shell command that removes what this call created, for example "
            "`docker rm -f db` for `docker run -d --name db postgres`.",
            "Absolute paths OUTSIDE the workspace that this call writes, for example ~/.zshrc. Empty if none.",
        )
    else:
        texts = (
            "这次调用对工作区以外的影响。工作区里的文件和安装的依赖回退时会自动恢复，只改它们填 none。"
            "reversible：只在工作区外新建了东西（容器、云资源、定时任务），并能用 reverse_command 删掉。"
            "irreversible：会发送、发布、部署，或覆盖、删除工作区外的东西。",
            "仅 reversible 时填写：删掉这次调用新建的东西的 shell 命令，例如 `docker run -d --name db postgres` "
            "对应 `docker rm -f db`。",
            "这次调用会写入的工作区以外的绝对路径，例如 ~/.zshrc；没有就留空。",
        )
    return {
        SIDE_EFFECT_ARG: {"type": "string", "enum": [c.value for c in (
            EffectCategory.NONE, EffectCategory.REVERSIBLE, EffectCategory.IRREVERSIBLE)], "description": texts[0]},
        REVERSE_COMMAND_ARG: {"type": "string", "description": texts[1]},
        EXTERNAL_PATHS_ARG: {"type": "array", "items": {"type": "string"}, "description": texts[2]},
    }


def parse_args(raw: Any) -> dict[str, Any]:
    """Tool arguments as a dict; the model sends them as a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _lookup(value: Any, path: list[str]) -> Any:
    for key in path:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = getattr(value, key, None)
        if value is None:
            return None
    return value


def result_data(result: Any) -> Any:
    """The payload of a tool result: ``ToolOutput.data`` or the plain value."""
    data = getattr(result, "data", None)
    return data if data is not None else result


def build_undo_args(declaration: EffectDeclaration, args: dict[str, Any], result: Any) -> dict[str, Any] | None:
    """Arguments for the undo tool, or ``None`` when a declared source is missing."""
    resolved = dict(declaration.undo_constants)
    for name, source in declaration.undo_args.items():
        root, _, rest = source.partition(".")
        value = _lookup(result_data(result) if root == "result" else args, rest.split("."))
        if value is None:
            return None
        resolved[name] = value
    return resolved


def is_failure(result: Any) -> bool:
    """A tool result that reports failure instead of raising."""
    success = result.get("success") if isinstance(result, dict) else getattr(result, "success", None)
    return success is False
