# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Side-effect declarations: what a tool does outside the workspace, and how to undo it.

Workspace files and dependency directories are not effects here; the workspace
snapshot restores them. A declaration answers one question per tool: does it
change anything else, and if so, can that change be undone?

Tools whose effect is the same on every call are declared once in a table, keyed
by tool name. ``BUILTIN_DECLARATIONS`` covers the harness's own tools; a host adds
or overrides entries for its tools. A tool missing from the table is irreversible.
"""

from __future__ import annotations

import json
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
"""Built-in harness tools. Shell and code execution are declared per call (U4); until
then they, like any tool missing here, count as irreversible."""


class DeclarationTable:
    """Looks up the declaration of a tool, host entries first."""

    def __init__(self, overrides: dict[str, EffectDeclaration] | None = None):
        self._table = {**BUILTIN_DECLARATIONS, **(overrides or {})}

    def get(self, tool_name: str) -> EffectDeclaration:
        return self._table.get(tool_name, IRREVERSIBLE)


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


def _result_data(result: Any) -> Any:
    """The payload of a tool result: ``ToolOutput.data`` or the plain value."""
    data = getattr(result, "data", None)
    return data if data is not None else result


def build_undo_args(declaration: EffectDeclaration, args: dict[str, Any], result: Any) -> dict[str, Any] | None:
    """Arguments for the undo tool, or ``None`` when a declared source is missing."""
    resolved = dict(declaration.undo_constants)
    for name, source in declaration.undo_args.items():
        root, _, rest = source.partition(".")
        value = _lookup(_result_data(result) if root == "result" else args, rest.split("."))
        if value is None:
            return None
        resolved[name] = value
    return resolved


def is_failure(result: Any) -> bool:
    """A tool result that reports failure instead of raising."""
    success = result.get("success") if isinstance(result, dict) else getattr(result, "success", None)
    return success is False
