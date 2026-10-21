"""07 - Agent frameworks: the same safe tools in LangChain, OpenAI Agents and the Claude Agent SDK.

Run:
    pip install -e ".[langchain,openai-agents,claude-agent]"
    python examples/07_frameworks.py

Each section builds the framework's native tools from one QueryPilot client and
skips itself when that framework is not installed. No API key is needed: the
tools are invoked directly to show the wiring; hand them to an agent as noted.
"""

from __future__ import annotations

from _common import demo_database_url

from querypilot import QueryPilot
from querypilot.integrations import (
    as_claude_agent_mcp_server,
    as_langchain_tools,
    as_openai_agents_tools,
    claude_agent_allowed_tools,
)

qp = QueryPilot.connect(demo_database_url(), max_rows=5)
SQL = "SELECT customer_name, arr FROM customers ORDER BY arr DESC"

try:  # LangChain / LangGraph: create_react_agent(model, tools)
    tools = as_langchain_tools(qp)  # naming="langchain" for sql_db_* drop-in names
    print("LangChain:", tools[-1].name, tools[-1].invoke({"sql": SQL})[:80], "...")
except ImportError as exc:
    print("LangChain skipped:", exc)

try:  # OpenAI Agents SDK: Agent(name="analyst", tools=tools, instructions=...)
    tools = as_openai_agents_tools(qp)
    print("OpenAI Agents:", [tool.name for tool in tools])
except ImportError as exc:
    print("OpenAI Agents skipped:", exc)

try:  # Claude Agent SDK: query(prompt, options=ClaudeAgentOptions(...))
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        mcp_servers={"querypilot": as_claude_agent_mcp_server(qp)},
        allowed_tools=claude_agent_allowed_tools("querypilot", qp=qp),
        system_prompt=qp.toolkit().instructions(),
    )
    print("Claude Agent SDK:", options.allowed_tools)
except ImportError as exc:
    print("Claude Agent SDK skipped:", exc)

# Any framework that takes plain functions (Pydantic AI, smolagents, CrewAI, ADK):
# from querypilot.integrations import as_functions; Agent(..., tools=as_functions(qp))
