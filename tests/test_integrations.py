from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
import typing

import pytest

from querypilot import QueryPilot
from querypilot.access import AccessPolicy
from querypilot.integrations import (
    as_claude_agent_mcp_server,
    as_functions,
    as_langchain_tools,
    as_llamaindex_tools,
    as_openai_agents_tools,
    claude_agent_allowed_tools,
)

CORE_TOOLS = ["list_tables", "describe_table", "search_schema", "validate_sql", "run_sql"]


@pytest.fixture()
def qp(demo_db_url: str) -> QueryPilot:
    return QueryPilot.connect(
        demo_db_url,
        max_rows=2,
        access_policy=AccessPolicy(blocked_columns={"customers": ["revenue"]}),
    )


def test_functions_are_typed_and_documented(qp: QueryPilot) -> None:
    functions = {function.__name__: function for function in as_functions(qp)}

    assert list(functions) == CORE_TOOLS
    run_sql = functions["run_sql"]
    hints = typing.get_type_hints(run_sql)
    assert hints["sql"] is str
    assert list(inspect.signature(run_sql).parameters) == ["sql"]
    assert "Args:\n    sql:" in run_sql.__doc__
    assert typing.get_type_hints(functions["describe_table"])["table_names"] == list[str]
    assert run_sql("SELECT customer_name FROM customers")["rows"] == [["Acme Corp"], ["Globex"]]
    assert run_sql("SELECT revenue FROM customers")["ok"] is False


@pytest.mark.parametrize(
    ("factory", "module", "extra"),
    [
        (as_langchain_tools, "langchain_core", "langchain"),
        (as_llamaindex_tools, "llama_index", "llamaindex"),
        (as_openai_agents_tools, "agents", "openai-agents"),
        (as_claude_agent_mcp_server, "claude_agent_sdk", "claude-agent"),
    ],
)
def test_missing_framework_names_the_extra(qp: QueryPilot, monkeypatch, factory, module, extra) -> None:
    real_import_module = importlib.import_module

    def fake_import_module(name, *args, **kwargs):
        if name.split(".")[0] == module:
            raise ImportError(f"No module named {name!r}")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match=re.escape(f'pip install "querypilot[{extra}]"')):
        factory(qp)


def test_langchain_tools_invoke(qp: QueryPilot) -> None:
    pytest.importorskip("langchain_core")
    from langchain_core.messages import ToolMessage

    tools = {tool.name: tool for tool in as_langchain_tools(qp)}

    assert list(tools) == CORE_TOOLS
    message = tools["run_sql"].invoke(
        {
            "name": "run_sql",
            "args": {"sql": "SELECT customer_name FROM customers"},
            "id": "call-1",
            "type": "tool_call",
        }
    )
    assert isinstance(message, ToolMessage)
    payload = json.loads(message.content)
    assert payload["rows"] == [["Acme Corp"], ["Globex"]]
    assert payload["truncated"] is True
    error = json.loads(tools["run_sql"].invoke({"sql": "DROP TABLE customers"}))
    assert error["ok"] is False
    described = json.loads(tools["describe_table"].invoke({"table_names": ["customers"]}))
    assert "revenue" not in json.dumps(described)


def test_langchain_drop_in_names(qp: QueryPilot) -> None:
    pytest.importorskip("langchain_core")

    tools = {tool.name: tool for tool in as_langchain_tools(qp, naming="langchain")}

    assert {"sql_db_list_tables", "sql_db_schema", "sql_db_query", "sql_db_query_checker"} <= set(tools)
    listed = json.loads(tools["sql_db_list_tables"].invoke(""))
    assert {table["name"] for table in listed["tables"]} == {"customers", "invoices"}
    schema = json.loads(tools["sql_db_schema"].invoke("customers, invoices"))
    assert [table["name"] for table in schema["tables"]] == ["customers", "invoices"]
    rows = json.loads(tools["sql_db_query"].invoke("SELECT COUNT(*) AS n FROM customers"))
    assert rows["rows"] == [[3]]
    checked = json.loads(tools["sql_db_query_checker"].invoke({"query": "DELETE FROM customers"}))
    assert checked["ok"] is False
    assert "sql_db_schema" in tools["sql_db_list_tables"].description


def test_llamaindex_tools_call(qp: QueryPilot) -> None:
    pytest.importorskip("llama_index.core")

    tools = {tool.metadata.name: tool for tool in as_llamaindex_tools(qp)}

    assert list(tools) == CORE_TOOLS
    schema = tools["run_sql"].metadata.get_parameters_dict()
    assert schema["required"] == ["sql"]
    assert schema["properties"]["sql"]["description"]
    output = tools["run_sql"].call(sql="SELECT customer_name FROM customers")
    assert json.loads(output.content)["row_count"] == 2
    listed = asyncio.run(tools["list_tables"].acall())
    assert json.loads(listed.content)["count"] == 2


def test_openai_agents_tools_invoke(qp: QueryPilot) -> None:
    agents = pytest.importorskip("agents")
    from agents.tool_context import ToolContext

    tools = {tool.name: tool for tool in as_openai_agents_tools(qp)}

    assert list(tools) == CORE_TOOLS
    run_sql = tools["run_sql"]
    assert isinstance(run_sql, agents.FunctionTool)
    assert run_sql.strict_json_schema is True
    assert run_sql.params_json_schema["additionalProperties"] is False
    arguments = json.dumps({"sql": "SELECT customer_name FROM customers"})
    context = ToolContext(
        context=None, tool_name="run_sql", tool_call_id="call-1", tool_arguments=arguments
    )
    output = asyncio.run(run_sql.on_invoke_tool(context, arguments))
    assert json.loads(output)["rows"] == [["Acme Corp"], ["Globex"]]
    bad = asyncio.run(run_sql.on_invoke_tool(context, '{"sql": "SELECT revenue FROM customers"}'))
    assert json.loads(bad)["ok"] is False


def test_claude_agent_sdk_server(qp: QueryPilot) -> None:
    pytest.importorskip("claude_agent_sdk")
    client_module = pytest.importorskip("mcp.client.client")

    config = as_claude_agent_mcp_server(qp)

    assert config["type"] == "sdk"
    assert config["name"] == "querypilot"
    assert claude_agent_allowed_tools(qp=qp) == [f"mcp__querypilot__{name}" for name in CORE_TOOLS]
    assert "mcp__qp__ask_database" in claude_agent_allowed_tools("qp")

    async def exercise():
        async with client_module.Client(config["instance"]) as client:
            listed = await client.list_tools()
            rows = await client.call_tool("run_sql", {"sql": "SELECT customer_name FROM customers"})
            failure = await client.call_tool("run_sql", {"sql": "DROP TABLE customers"})
            return listed.tools, rows, failure

    tools, rows, failure = asyncio.run(exercise())

    assert [tool.name for tool in tools] == CORE_TOOLS
    assert all(tool.annotations.read_only_hint for tool in tools)
    assert json.loads(rows.content[0].text)["rows"] == [["Acme Corp"], ["Globex"]]
    assert failure.is_error is True
    assert json.loads(failure.content[0].text)["ok"] is False
