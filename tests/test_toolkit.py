from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from querypilot import QueryPilot, QueryPilotToolkit
from querypilot.access import AccessPolicy, MaskingRule
from querypilot.core.types import GeneratedSQL

CORE_TOOLS = ["list_tables", "describe_table", "search_schema", "validate_sql", "run_sql"]


class FixedGenerator:
    def generate(self, question, schema, max_rows):
        return GeneratedSQL(question=question, sql="SELECT customer_name FROM customers")


def test_toolkit_lists_core_tools_and_hides_ask_for_demo_generator(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url)

    assert qp.toolkit().tool_names == CORE_TOOLS
    assert qp.toolkit(include_ask=True).tool_names == [*CORE_TOOLS, "ask_database"]
    with_generator = QueryPilot.connect(demo_db_url, generator=FixedGenerator())
    assert "ask_database" in with_generator.toolkit().tool_names
    assert "ask_database" not in with_generator.toolkit(include_ask=False).tool_names


def test_provider_formats_share_one_definition(demo_db_url: str) -> None:
    toolkit = QueryPilot.connect(demo_db_url, max_rows=7).toolkit()

    chat = toolkit.openai_tools()
    responses = toolkit.openai_responses_tools()
    anthropic = toolkit.anthropic_tools()

    assert [tool["function"]["name"] for tool in chat] == CORE_TOOLS
    assert [tool["name"] for tool in responses] == CORE_TOOLS
    assert [tool["name"] for tool in anthropic] == CORE_TOOLS
    run_sql = next(tool for tool in responses if tool["name"] == "run_sql")
    assert run_sql["type"] == "function"
    assert run_sql["strict"] is True
    assert "7 rows" in run_sql["description"]
    for tool in chat:
        parameters = tool["function"]["parameters"]
        assert tool["function"]["strict"] is True
        assert parameters["additionalProperties"] is False
        assert sorted(parameters["required"]) == sorted(parameters["properties"])
    describe = next(tool for tool in anthropic if tool["name"] == "describe_table")
    assert describe["input_schema"]["properties"]["table_names"]["type"] == "array"


def test_list_tables_hides_blocked_and_unlisted_tables(demo_db_url: str) -> None:
    blocked = QueryPilot.connect(demo_db_url, blocked_tables=["invoices"])
    allowed = QueryPilot.connect(demo_db_url, allowed_tables=["invoices"])

    blocked_result = blocked.handle_tool_call("list_tables", {})
    allowed_result = allowed.handle_tool_call("list_tables", {})

    assert blocked_result["ok"] is True
    assert [table["name"] for table in blocked_result["tables"]] == ["customers"]
    assert blocked_result["tables"][0]["columns"] == 4
    assert [table["name"] for table in allowed_result["tables"]] == ["invoices"]


def test_describe_table_hides_policy_columns_and_masked_samples(demo_db_url: str) -> None:
    qp = QueryPilot.connect(
        demo_db_url,
        blocked_tables=["invoices"],
        access_policy=AccessPolicy(
            blocked_columns={"customers": ["revenue"]},
            masking_rules={"customers": {"customer_name": MaskingRule(mode="redact")}},
        ),
    )

    result = qp.handle_tool_call("describe_table", {"table_names": ["customers", "invoices"]})

    assert result["ok"] is True
    [table] = result["tables"]
    columns = {column["name"]: column for column in table["columns"]}
    assert set(columns) == {"id", "customer_name", "arr"}
    assert columns["id"]["primary_key"] is True
    assert columns["customer_name"]["masked"] == "redact"
    assert "sample_values" not in columns["customer_name"]
    # A blocked table is reported exactly like a table that does not exist.
    assert result["unknown_tables"] == ["invoices"]
    serialized = json.dumps(result)
    assert "revenue" not in serialized
    assert "Acme" not in serialized


def test_describe_table_shows_sample_values(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url)

    result = qp.handle_tool_call("describe_table", {"table_names": "customers"})

    columns = {column["name"]: column for column in result["tables"][0]["columns"]}
    assert "Acme Corp" in columns["customer_name"]["sample_values"]
    assert columns["customer_name"]["nullable"] is False


def test_describe_table_allowed_columns_and_foreign_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "fk.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, secret TEXT);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            account_id INTEGER REFERENCES accounts(id),
            total INTEGER
        );
        """
    )
    conn.close()
    qp = QueryPilot.connect(
        f"sqlite:///{db_path}",
        access_policy=AccessPolicy(allowed_columns={"accounts": ["id", "name"]}),
    )

    result = qp.handle_tool_call("describe_table", '{"table_names": ["orders", "accounts"]}')

    tables = {table["name"]: table for table in result["tables"]}
    assert [column["name"] for column in tables["accounts"]["columns"]] == ["id", "name"]
    assert tables["orders"]["foreign_keys"] == [
        {"columns": ["account_id"], "references": "accounts(id)"}
    ]


def test_describe_unknown_table_suggests_close_names(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url)

    result = qp.handle_tool_call("describe_table", {"table_names": ["customer"]})

    assert result["ok"] is False
    assert result["error"] == "Unknown table: customer"
    assert any("customers" in hint for hint in result["hints"])


def test_run_sql_returns_compact_rows(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url)

    result = qp.handle_tool_call(
        "run_sql", {"sql": "SELECT customer_name, arr FROM customers ORDER BY arr DESC"}
    )

    assert result == {
        "ok": True,
        "sql": result["sql"],
        "columns": ["customer_name", "arr"],
        "rows": [["Acme Corp", 120000], ["Globex", 95000], ["Initech", 40000]],
        "row_count": 3,
        "truncated": False,
        "warnings": [],
    }
    assert result["sql"].endswith("LIMIT 100")


def test_run_sql_flags_row_limit(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, max_rows=2)

    result = qp.handle_tool_call("run_sql", {"sql": "SELECT customer_name FROM customers"})

    assert result["ok"] is True
    assert result["row_count"] == 2
    assert result["truncated"] is True
    assert "aggregate" in result["note"].lower()


def test_run_sql_caps_cells_and_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "big.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO notes (body) VALUES (?)", [("x" * 5000,) for _ in range(100)])
    conn.commit()
    conn.close()
    qp = QueryPilot.connect(f"sqlite:///{db_path}", max_rows=100)

    result = qp.toolkit(max_payload_chars=5000, max_cell_chars=200).dispatch(
        "run_sql", {"sql": "SELECT id, body FROM notes"}
    )

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["row_count"] == 100
    assert 0 < result["rows_shown"] == len(result["rows"]) < 100
    assert all(len(row[1]) <= 200 for row in result["rows"])
    assert len(json.dumps(result)) <= 5000
    assert "shortened" in result["note"]


def test_failures_are_payloads_not_exceptions(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, access_policy=AccessPolicy(blocked_columns={"customers": ["revenue"]}))

    dropped = qp.handle_tool_call("run_sql", {"sql": "DROP TABLE customers"})
    blocked = qp.handle_tool_call("run_sql", {"sql": "SELECT revenue FROM customers"})
    unknown_column = qp.handle_tool_call("run_sql", {"sql": "SELECT custmer_name FROM customers"})
    unknown_tool = qp.handle_tool_call("drop_everything", {})
    missing_argument = qp.handle_tool_call("run_sql", {})
    bad_json = qp.handle_tool_call("run_sql", "{not json")

    for result in (dropped, blocked, unknown_column, unknown_tool, missing_argument, bad_json):
        assert result["ok"] is False
        assert isinstance(result["error"], str) and result["error"]
        assert isinstance(result["hints"], list)
    assert dropped["error"].startswith("SQL validation failed")
    assert "customers.revenue" in blocked["error"]
    assert "Unknown tool" in unknown_tool["error"]
    assert "run_sql" in unknown_tool["hints"][0]
    assert "sql" in missing_argument["error"]
    assert "JSON" in bad_json["error"]


def test_database_errors_become_payloads(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url)

    def explode(sql: str):
        raise RuntimeError("connection reset by peer")

    qp.connector.execute_readonly = explode

    result = qp.handle_tool_call("run_sql", {"sql": "SELECT customer_name FROM customers"})

    assert result["ok"] is False
    assert result["error"] == "Database error: connection reset by peer"


def test_validate_sql_is_compact(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url)

    valid = qp.handle_tool_call("validate_sql", {"sql": "SELECT * FROM customers"})
    invalid = qp.handle_tool_call("validate_sql", {"sql": "DELETE FROM customers"})

    assert set(valid) == {"ok", "sql", "errors", "hints", "warnings"}
    assert valid["ok"] is True
    assert valid["sql"].endswith("LIMIT 100")
    assert valid["warnings"]
    assert invalid["ok"] is False
    assert invalid["errors"]
    assert len(json.dumps(valid)) < 400


def test_search_schema_hides_blocked_columns(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, access_policy=AccessPolicy(blocked_columns={"customers": ["revenue"]}))

    result = qp.handle_tool_call("search_schema", {"query": "revenue"})

    assert result["ok"] is True
    assert {"table": "customers", "column": "revenue"} not in [
        {key: match.get(key) for key in ("table", "column")} for match in result["matches"]
    ]


def test_execute_sql_alias_and_ask_database(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, generator=FixedGenerator())

    alias = qp.handle_tool_call("execute_sql", {"sql": "SELECT customer_name FROM customers"})
    answer = qp.handle_tool_call("ask_database", {"question": "List customers"})

    assert alias["ok"] is True and alias["row_count"] == 3
    assert answer["ok"] is True
    assert answer["rows"] == [["Acme Corp"], ["Globex"], ["Initech"]]
    assert answer["explanation"]


def test_handle_anthropic_tool_call_delegates(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url)

    result = qp.handle_anthropic_tool_call("search_schema", {"query": "customers"})
    unknown = qp.handle_anthropic_tool_call("drop_everything", {})

    assert result["matches"][0]["table"] == "customers"
    assert unknown["ok"] is False


def test_instructions_describe_workflow(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, hints=["ARR means annual recurring revenue."])

    instructions = qp.toolkit().instructions()

    assert "list_tables" in instructions and "describe_table" in instructions
    assert "ARR means annual recurring revenue." in instructions


def test_toolkit_renames_tools(demo_db_url: str) -> None:
    toolkit = QueryPilotToolkit(QueryPilot.connect(demo_db_url), names={"run_sql": "query_db"})

    assert "query_db" in toolkit.tool_names
    assert "query_db" in toolkit.get("validate_sql").description
    assert "query_db" in toolkit.instructions()
    assert toolkit.dispatch("query_db", {"sql": "SELECT 1"})["ok"] is True
    assert toolkit.dispatch("run_sql", {"sql": "SELECT 1"})["ok"] is True


@pytest.mark.parametrize("arguments", [None, "", {}])
def test_list_tables_accepts_empty_arguments(demo_db_url: str, arguments) -> None:
    qp = QueryPilot.connect(demo_db_url)

    assert qp.handle_tool_call("list_tables", arguments)["count"] == 2
