"""QueryPilot tools for the OpenAI Agents SDK.

    from agents import Agent
    agent = Agent(name="analyst", tools=as_openai_agents_tools(qp))
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from querypilot.adapters.toolkit import QueryPilotTool, QueryPilotToolkit
from querypilot.integrations._imports import require
from querypilot.integrations.functions import resolve_toolkit

if TYPE_CHECKING:
    from querypilot.core.client import QueryPilot


def as_openai_agents_tools(
    querypilot: QueryPilot | QueryPilotToolkit, **toolkit_options: Any
) -> list[Any]:
    """Return an ``agents.FunctionTool`` (strict JSON schema) per QueryPilot tool."""
    agents = require("agents", "openai-agents")
    toolkit = resolve_toolkit(querypilot, **toolkit_options)
    return [
        agents.FunctionTool(
            name=tool.name,
            description=tool.description,
            params_json_schema=tool.parameters,
            on_invoke_tool=_invoker(toolkit, tool),
            strict_json_schema=True,
        )
        for tool in toolkit.tools
    ]


def _invoker(toolkit: QueryPilotToolkit, tool: QueryPilotTool):
    async def on_invoke_tool(context: Any, arguments: str) -> str:
        # Database calls block, so keep them off the event loop.
        return await asyncio.to_thread(toolkit.dispatch_json, tool.name, arguments)

    return on_invoke_tool
