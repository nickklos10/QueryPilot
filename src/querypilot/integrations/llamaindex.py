"""QueryPilot tools for LlamaIndex agents.

    from llama_index.core.agent.workflow import FunctionAgent
    agent = FunctionAgent(tools=as_llamaindex_tools(qp), llm=llm)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from querypilot.adapters.toolkit import QueryPilotToolkit
from querypilot.integrations._imports import require
from querypilot.integrations.functions import args_model, resolve_toolkit, tool_function

if TYPE_CHECKING:
    from querypilot.core.client import QueryPilot


def as_llamaindex_tools(
    querypilot: QueryPilot | QueryPilotToolkit, **toolkit_options: Any
) -> list[Any]:
    """Return a ``FunctionTool`` per QueryPilot tool; each returns a JSON string."""
    tools_module = require("llama_index.core.tools", "llamaindex")
    toolkit = resolve_toolkit(querypilot, **toolkit_options)
    return [
        tools_module.FunctionTool.from_defaults(
            fn=tool_function(toolkit, tool, as_json=True),
            name=tool.name,
            description=tool.description,
            fn_schema=args_model(tool.name, tool.parameters),
        )
        for tool in toolkit.tools
    ]
