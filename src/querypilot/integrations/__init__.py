"""Hook QueryPilot's tools into agent frameworks. Framework packages are imported lazily."""
from querypilot.integrations.claude_agent import (
    as_claude_agent_mcp_server,
    claude_agent_allowed_tools,
)
from querypilot.integrations.functions import as_functions
from querypilot.integrations.langchain import as_langchain_tools
from querypilot.integrations.llamaindex import as_llamaindex_tools
from querypilot.integrations.openai_agents import as_openai_agents_tools

__all__ = [
    "as_claude_agent_mcp_server",
    "as_functions",
    "as_langchain_tools",
    "as_llamaindex_tools",
    "as_openai_agents_tools",
    "claude_agent_allowed_tools",
]
