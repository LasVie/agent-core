# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MCP tool cards keep the hints the server gives about each tool (MCP ToolAnnotations)."""
from __future__ import annotations

import sys

import pytest
from mcp.types import Tool, ToolAnnotations

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.foundation.tool.mcp.base import mcp_tool_annotations
from openjiuwen.core.foundation.tool.mcp.client.stdio_client import StdioClient

NOTES_SERVER = '''
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("notes")
log = Path(sys.argv[1])


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def lookup(key: str) -> str:
    """Read a note."""
    return f"note {key}"


@server.tool(annotations=ToolAnnotations(destructiveHint=False))
def write(key: str, value: str) -> str:
    """Write a note."""
    with log.open("a") as handle:
        handle.write(f"{key}={value}\\n")
    return "written"


server.run()
'''


def notes_server_config(tmp_path, server_name: str = "notes") -> McpServerConfig:
    """A local stdio MCP server: ``lookup`` is marked read-only, ``write`` appends to ``writes.log``."""
    script = tmp_path / "notes_server.py"
    script.write_text(NOTES_SERVER)
    return McpServerConfig(
        server_name=server_name, server_path="stdio://notes", client_type="stdio",
        params={"command": sys.executable, "args": [str(script), str(tmp_path / "writes.log")]},
    )


def test_annotations_become_a_plain_dict_without_unset_hints():
    hinted = Tool(name="a", inputSchema={"type": "object"}, annotations=ToolAnnotations(readOnlyHint=True))
    assert mcp_tool_annotations(hinted) == {"readOnlyHint": True}
    assert mcp_tool_annotations(Tool(name="b", inputSchema={"type": "object"})) is None
    assert mcp_tool_annotations(type("Raw", (), {"annotations": {"readOnlyHint": False}})()) == {"readOnlyHint": False}


@pytest.mark.asyncio
async def test_listed_tool_cards_carry_the_servers_hints(tmp_path):
    client = StdioClient(notes_server_config(tmp_path))
    assert await client.connect()
    try:
        cards = {card.name: card for card in await client.list_tools()}
    finally:
        await client.disconnect()

    assert cards["lookup"].annotations == {"readOnlyHint": True}
    assert cards["write"].annotations == {"destructiveHint": False}
    assert "annotations" not in cards["lookup"].tool_info().model_dump(), "what the model sees does not change"
