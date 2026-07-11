from __future__ import annotations

from querypilot import QueryPilot
from querypilot.access import AccessPolicy
from querypilot.mcp.server import create_mcp_server


class FakeFastMCP:
    def __init__(self, name: str, **kwargs) -> None:
        self.name = name
        self.kwargs = kwargs
        self.tools = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def test_mcp_server_registers_querypilot_tools(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2)

    server = create_mcp_server(qp, fastmcp_cls=FakeFastMCP)

    assert server.name == "QueryPilot"
    assert set(server.tools) == {
        "ask_database",
        "search_schema",
        "validate_sql",
        "execute_sql",
    }
    validation = server.tools["validate_sql"]("SELECT * FROM customers")
    result = server.tools["execute_sql"]("SELECT customer_name FROM customers")

    assert validation["valid"] is True
    assert result["row_count"] == 2


def test_mcp_server_tool_errors_are_structured(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")
    server = create_mcp_server(qp, fastmcp_cls=FakeFastMCP)

    result = server.tools["execute_sql"]("DROP TABLE customers")

    assert result["error"].startswith("SQL validation failed")


def test_mcp_access_policy_cannot_be_bypassed_by_alias_or_star(
    demo_db_url: str,
) -> None:
    qp = QueryPilot.connect(
        demo_db_url,
        dialect="sqlite",
        access_policy=AccessPolicy(blocked_columns={"customers": ["revenue"]}),
    )
    server = create_mcp_server(qp, fastmcp_cls=FakeFastMCP)

    alias_result = server.tools["execute_sql"](
        "SELECT c.revenue FROM customers AS c"
    )
    star_result = server.tools["execute_sql"]("SELECT * FROM customers")

    assert alias_result["error"].startswith("SQL validation failed")
    assert "customers.revenue" in alias_result["error"]
    assert star_result["error"].startswith("SQL validation failed")
    assert "customers.revenue" in star_result["error"]
    assert set(server.tools) == {
        "ask_database",
        "search_schema",
        "validate_sql",
        "execute_sql",
    }
