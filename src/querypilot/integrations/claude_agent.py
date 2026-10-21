"""QueryPilot tools for the Claude Agent SDK, as an in-process MCP server.

    options = ClaudeAgentOptions(
        mcp_servers={"querypilot": as_claude_agent_mcp_server(qp)},
        allowed_tools=claude_agent_allowed_tools(qp=qp),
    )
"""
from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

from querypilot.adapters.toolkit import (
    READ_ONLY_ANNOTATIONS,
    TOOL_KEYS,
    QueryPilotTool,
    QueryPilotToolkit,
    to_json,
)
from querypilot.integrations._imports import require
from querypilot.integrations.functions import resolve_toolkit

if TYPE_CHECKING:
    from querypilot.core.client import QueryPilot


def as_claude_agent_mcp_server(
    querypilot: QueryPilot | QueryPilotToolkit,
    name: str = "querypilot",
    **toolkit_options: Any,
) -> Any:
    """Return an SDK MCP server config for ``ClaudeAgentOptions(mcp_servers=...)``."""
    sdk = require("claude_agent_sdk", "claude-agent")
    toolkit = resolve_toolkit(querypilot, **toolkit_options)
    supports_annotations = "annotations" in inspect.signature(sdk.tool).parameters
    tools = []
    for tool in toolkit.tools:
        decorator_kwargs: dict[str, Any] = {}
        if supports_annotations:
            decorator_kwargs["annotations"] = _annotations(tool)
        decorator = sdk.tool(tool.name, tool.description, tool.parameters, **decorator_kwargs)
        tools.append(decorator(_handler(toolkit, tool)))
    return sdk.create_sdk_mcp_server(name=name, tools=tools)


def claude_agent_allowed_tools(
    name: str = "querypilot", *, qp: QueryPilot | QueryPilotToolkit | None = None
) -> list[str]:
    """Tool names to pass as ``ClaudeAgentOptions(allowed_tools=...)``."""
    if qp is None:
        tool_names = list(TOOL_KEYS)
    else:
        tool_names = resolve_toolkit(qp).tool_names
    return [f"mcp__{name}__{tool_name}" for tool_name in tool_names]


def _handler(toolkit: QueryPilotToolkit, tool: QueryPilotTool):
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = await asyncio.to_thread(toolkit.dispatch, tool.name, arguments)
        return {
            "content": [{"type": "text", "text": to_json(payload)}],
            "is_error": payload.get("ok") is False,
        }

    return handler


def _annotations(tool: QueryPilotTool) -> Any:
    from mcp.types import ToolAnnotations

    return ToolAnnotations(title=tool.title, **READ_ONLY_ANNOTATIONS)
