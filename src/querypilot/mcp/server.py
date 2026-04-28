from __future__ import annotations

from typing import Any, Callable

from querypilot import QueryPilot


def create_mcp_server(
    querypilot: QueryPilot,
    *,
    fastmcp_cls: type | None = None,
):
    fastmcp = fastmcp_cls or _load_fastmcp()
    server = fastmcp("QueryPilot", stateless_http=True, json_response=True)

    @server.tool()
    def ask_database(question: str) -> dict[str, Any]:
        """Ask a database question through QueryPilot's safe SQL flow."""
        return _safe_call(lambda: querypilot.ask(question).model_dump())

    @server.tool()
    def search_schema(query: str) -> list[dict[str, Any]] | dict[str, str]:
        """Search tables and columns relevant to a question."""
        return _safe_call(lambda: [match.model_dump() for match in querypilot.search_schema(query)])

    @server.tool()
    def validate_sql(sql: str) -> dict[str, Any]:
        """Validate SQL and return policy metadata."""
        return _safe_call(lambda: querypilot.validate_sql(sql).model_dump())

    @server.tool()
    def execute_sql(sql: str) -> dict[str, Any]:
        """Validate and execute read-only SQL safely."""
        return _safe_call(lambda: querypilot.execute_sql(sql).model_dump())

    return server


def _safe_call(callback: Callable[[], Any]) -> Any:
    try:
        return callback()
    except ValueError as exc:
        return {"error": str(exc)}


def _load_fastmcp():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError("Install querypilot[mcp] to run the MCP server.") from exc
    return FastMCP
