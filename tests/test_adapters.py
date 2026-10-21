from __future__ import annotations

from querypilot import QueryPilot
from querypilot.adapters.anthropic import anthropic_tools
from querypilot.adapters.openai import openai_tools

CORE_TOOLS = {"list_tables", "describe_table", "search_schema", "validate_sql", "run_sql"}


def test_openai_tools_have_expected_shape(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    tools = qp.as_openai_tools()

    names = {tool["function"]["name"] for tool in tools}
    assert names == CORE_TOOLS
    run_tool = next(tool for tool in tools if tool["function"]["name"] == "run_sql")
    assert run_tool["type"] == "function"
    assert run_tool["function"]["parameters"]["properties"]["sql"]["type"] == "string"


def test_anthropic_tools_have_expected_shape(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    tools = qp.as_anthropic_tools()

    names = {tool["name"] for tool in tools}
    assert names == CORE_TOOLS
    describe_tool = next(tool for tool in tools if tool["name"] == "describe_table")
    assert describe_tool["input_schema"]["properties"]["table_names"]["items"]["type"] == "string"


def test_module_level_tool_lists_include_every_tool() -> None:
    assert {tool["function"]["name"] for tool in openai_tools()} == CORE_TOOLS | {"ask_database"}
    assert {tool["name"] for tool in anthropic_tools()} == CORE_TOOLS | {"ask_database"}


def test_anthropic_handler_dispatches_and_reports_unknown_tools(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.handle_anthropic_tool_call("search_schema", {"query": "customers"})
    unknown = qp.handle_anthropic_tool_call("drop_everything", {})

    assert result["matches"][0]["table"] == "customers"
    assert unknown == {
        "ok": False,
        "error": "Unknown tool: drop_everything",
        "hints": [unknown["hints"][0]],
    }
    assert "run_sql" in unknown["hints"][0]


def test_handle_tool_call_accepts_openai_json_arguments(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.handle_tool_call("run_sql", '{"sql": "SELECT customer_name FROM customers"}')

    assert result["ok"] is True
    assert result["row_count"] == 3
