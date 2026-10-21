"""QueryPilot tools for LangChain and LangGraph.

    from langgraph.prebuilt import create_react_agent
    agent = create_react_agent(model, as_langchain_tools(qp))

``naming="langchain"`` reuses the names of langchain-community's ``SQLDatabaseToolkit``
(``sql_db_list_tables``, ``sql_db_schema``, ``sql_db_query``, ``sql_db_query_checker``),
so prompts written for that toolkit keep working.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from querypilot.adapters.toolkit import QueryPilotTool, QueryPilotToolkit, to_json
from querypilot.integrations._imports import require
from querypilot.integrations.functions import args_model, resolve_toolkit, tool_function

if TYPE_CHECKING:
    from querypilot.core.client import QueryPilot

LANGCHAIN_NAMES = {
    "list_tables": "sql_db_list_tables",
    "describe_table": "sql_db_schema",
    "run_sql": "sql_db_query",
    "validate_sql": "sql_db_query_checker",
}


def as_langchain_tools(
    querypilot: QueryPilot | QueryPilotToolkit,
    *,
    naming: Literal["querypilot", "langchain"] = "querypilot",
    **toolkit_options: Any,
) -> list[Any]:
    """Return a ``StructuredTool`` per QueryPilot tool."""
    tools_module = require("langchain_core.tools", "langchain")
    if naming not in {"querypilot", "langchain"}:
        raise ValueError("naming must be 'querypilot' or 'langchain'.")
    if naming == "langchain":
        if isinstance(querypilot, QueryPilotToolkit):
            raise TypeError("naming='langchain' needs a QueryPilot client, not a toolkit.")
        toolkit_options.setdefault("names", LANGCHAIN_NAMES)
    toolkit = resolve_toolkit(querypilot, **toolkit_options)

    tools = []
    for tool in toolkit.tools:
        if naming == "langchain" and tool.key in LANGCHAIN_NAMES:
            func, description, json_schema = _langchain_style(toolkit, tool)
            schema = args_model(tool.name, json_schema)
        else:
            func = tool_function(toolkit, tool, as_json=True)
            description, schema = tool.description, args_model(tool.name, tool.parameters)
        tools.append(
            tools_module.StructuredTool.from_function(
                func=func,
                name=tool.name,
                description=description,
                args_schema=schema,
                infer_schema=False,
            )
        )
    return tools


def _langchain_style(
    toolkit: QueryPilotToolkit, tool: QueryPilotTool
) -> tuple[Callable[..., str], str, dict[str, Any]]:
    """Signatures matching langchain-community's SQL tools: string inputs only."""
    name = tool.name
    if tool.key == "list_tables":

        def func(tool_input: str = "") -> str:
            return to_json(toolkit.dispatch(name, {}))

        return func, tool.description, _schema({"tool_input": ("An empty string.", "")})

    if tool.key == "describe_table":

        def func(table_names: str) -> str:
            return to_json(toolkit.dispatch(name, {"table_names": table_names}))

        description = (
            f"{tool.description} Input is a comma-separated list of tables, "
            "for example: table1, table2, table3"
        )
        return func, description, _schema({"table_names": ("Comma-separated table names.", None)})

    def func(query: str) -> str:
        return to_json(toolkit.dispatch(name, {"sql": query}))

    return func, tool.description, _schema({"query": ("A single SELECT statement.", None)})


def _schema(properties: dict[str, tuple[str, str | None]]) -> dict[str, Any]:
    return {
        "properties": {
            param: {"type": "string", "description": description, "default": default}
            for param, (description, default) in properties.items()
        },
        "required": [param for param, (_, default) in properties.items() if default is None],
    }

