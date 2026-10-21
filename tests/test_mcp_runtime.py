from __future__ import annotations

import asyncio
import json

import pytest

from querypilot import QueryPilot
from querypilot.access import AccessPolicy
from querypilot.core.types import GeneratedSQL
from querypilot.mcp.server import create_mcp_server

CORE_TOOLS = {"list_tables", "describe_table", "search_schema", "validate_sql", "run_sql"}


class FakeFastMCP:
    def __init__(self, name: str, **kwargs) -> None:
        self.name = name
        self.kwargs = kwargs
        self.tools = {}
        self.raw_tools = {}
        self.options = {}

    def tool(self, **options):
        def decorator(func):
            name = options.get("name", func.__name__)
            # Tools return compact JSON text; decode it so tests can assert on fields.
            self.tools[name] = lambda *args, **kwargs: json.loads(func(*args, **kwargs))
            self.raw_tools[name] = func
            self.options[name] = options
            return func

        return decorator


def _hints(annotations) -> dict:
    """Annotation hints by wire name, for dicts and mcp 1.x / 2.x ``ToolAnnotations``."""
    if isinstance(annotations, dict):
        return annotations
    return annotations.model_dump(by_alias=True, exclude_none=True)


class FixedGenerator:
    def generate(self, question, schema, max_rows):
        return GeneratedSQL(question=question, sql="SELECT customer_name FROM customers")


def test_mcp_server_registers_querypilot_tools(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2)

    server = create_mcp_server(qp, fastmcp_cls=FakeFastMCP)

    assert server.name == "QueryPilot"
    assert "list_tables" in server.kwargs["instructions"]
    assert set(server.tools) == CORE_TOOLS
    validation = server.tools["validate_sql"]("SELECT * FROM customers")
    result = server.tools["run_sql"]("SELECT customer_name FROM customers")
    tables = server.tools["list_tables"]()
    raw = server.raw_tools["list_tables"]()
    assert isinstance(raw, str) and "\n" not in raw

    assert validation["ok"] is True
    assert result["row_count"] == 2
    assert result["truncated"] is True
    assert {table["name"] for table in tables["tables"]} == {"customers", "invoices"}
    annotations = _hints(server.options["run_sql"]["annotations"])
    assert annotations["readOnlyHint"] is True
    assert annotations["destructiveHint"] is False


def test_mcp_server_registers_ask_with_a_real_generator(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, generator=FixedGenerator())

    server = create_mcp_server(qp, fastmcp_cls=FakeFastMCP)

    assert set(server.tools) == CORE_TOOLS | {"ask_database"}
    assert server.tools["ask_database"]("List customers")["row_count"] == 3


def test_mcp_server_tool_errors_are_structured(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")
    server = create_mcp_server(qp, fastmcp_cls=FakeFastMCP)

    result = server.tools["run_sql"]("DROP TABLE customers")

    assert result["ok"] is False
    assert result["error"].startswith("SQL validation failed")
    assert isinstance(result["hints"], list)


def test_mcp_access_policy_cannot_be_bypassed_by_alias_or_star(
    demo_db_url: str,
) -> None:
    qp = QueryPilot.connect(
        demo_db_url,
        dialect="sqlite",
        access_policy=AccessPolicy(blocked_columns={"customers": ["revenue"]}),
    )
    server = create_mcp_server(qp, fastmcp_cls=FakeFastMCP)

    alias_result = server.tools["run_sql"]("SELECT c.revenue FROM customers AS c")
    star_result = server.tools["run_sql"]("SELECT * FROM customers")

    assert alias_result["error"].startswith("SQL validation failed")
    assert "revenue" not in alias_result["error"]
    assert "restricted by the access policy" in alias_result["error"]
    assert star_result["error"].startswith("SQL validation failed")
    # SELECT * would otherwise reveal the name of a column describe_table hides.
    assert "revenue" not in json.dumps(star_result)
    assert "restricted by the access policy" in star_result["error"]
    described = server.tools["describe_table"](["customers"])
    assert "revenue" not in json.dumps(described)


def test_real_mcp_server_annotations_and_calls(demo_db_url: str) -> None:
    pytest.importorskip("mcp")
    client_module = pytest.importorskip("mcp.client.client")
    qp = QueryPilot.connect(demo_db_url, max_rows=2)
    server = create_mcp_server(qp)

    async def exercise():
        async with client_module.Client(server) as client:
            listed = await client.list_tools()
            tables = await client.call_tool("list_tables", {})
            rows = await client.call_tool("run_sql", {"sql": "SELECT customer_name FROM customers"})
            failure = await client.call_tool("run_sql", {"sql": "DELETE FROM customers"})
            return listed.tools, tables, rows, failure

    tools, tables, rows, failure = asyncio.run(exercise())

    assert {tool.name for tool in tools} == CORE_TOOLS
    for tool in tools:
        assert _hints(tool.annotations) == {
            "title": _hints(tool.annotations)["title"],
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    assert {table["name"] for table in json.loads(tables.content[0].text)["tables"]} == {
        "customers",
        "invoices",
    }
    payload = json.loads(rows.content[0].text)
    assert payload["rows"] == [["Acme Corp"], ["Globex"]]
    assert payload["truncated"] is True
    assert json.loads(failure.content[0].text)["ok"] is False
    assert server.instructions and "describe_table" in server.instructions
