from __future__ import annotations

import pytest

from querypilot import QueryPilot


def test_openai_tools_have_expected_shape(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    tools = qp.as_openai_tools()

    names = {tool["function"]["name"] for tool in tools}
    assert names == {"ask_database", "search_schema", "validate_sql", "execute_sql"}
    ask_tool = next(tool for tool in tools if tool["function"]["name"] == "ask_database")
    assert ask_tool["type"] == "function"
    assert ask_tool["function"]["parameters"]["properties"]["question"]["type"] == "string"


def test_anthropic_tools_have_expected_shape(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    tools = qp.as_anthropic_tools()

    names = {tool["name"] for tool in tools}
    assert names == {"ask_database", "search_schema", "validate_sql", "execute_sql"}
    ask_tool = next(tool for tool in tools if tool["name"] == "ask_database")
    assert ask_tool["input_schema"]["properties"]["question"]["type"] == "string"


def test_anthropic_handler_dispatches_and_rejects_unknown_tools(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.handle_anthropic_tool_call(
        "search_schema",
        {"query": "customers"},
    )

    assert result[0]["table"] == "customers"

    with pytest.raises(ValueError, match="Unknown Anthropic tool"):
        qp.handle_anthropic_tool_call("drop_everything", {})
