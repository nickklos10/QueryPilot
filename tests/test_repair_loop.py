"""The ask() repair loop: validation, execution-error and empty-result feedback."""
from __future__ import annotations

import json

import pytest

from querypilot import QueryPilot
from querypilot.access import AccessPolicy
from querypilot.core.client import EMPTY_RESULT_FEEDBACK
from querypilot.core.types import DatabaseSchema, GeneratedSQL, ValidationResult
from querypilot.generation.llm import OpenAICompatibleSQLGenerator


class ScriptedGenerator:
    """Returns the scripted SQL in order and records what repair() was given."""

    def __init__(self, *sqls: str | None) -> None:
        self._sqls = list(sqls)
        self.schemas: list[DatabaseSchema] = []
        self.repairs: list[tuple[str, ValidationResult]] = []

    def _next(self, question: str) -> GeneratedSQL:
        sql = self._sqls.pop(0) if len(self._sqls) > 1 else self._sqls[0]
        if sql is None:
            return GeneratedSQL(question=question, sql=None, errors=["model refused"])
        return GeneratedSQL(question=question, sql=sql, explanation="scripted")

    def generate(self, question: str, schema: DatabaseSchema, max_rows: int) -> GeneratedSQL:
        self.schemas.append(schema)
        return self._next(question)

    def repair(self, question, schema, max_rows, previous_sql, validation) -> GeneratedSQL:
        self.schemas.append(schema)
        self.repairs.append((previous_sql, validation))
        return self._next(question)


class GenerateOnly:
    def __init__(self, sql: str) -> None:
        self.sql = sql

    def generate(self, question: str, schema: DatabaseSchema, max_rows: int) -> GeneratedSQL:
        return GeneratedSQL(question=question, sql=self.sql)


def _chat(content: str):
    message = type("Message", (), {"content": content})()
    choice = type("Choice", (), {"message": message})()
    return type("ChatCompletion", (), {"choices": [choice]})()


class FakeChatClient:
    def __init__(self, contents: list[str]) -> None:
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _chat(contents[min(len(outer.calls), len(contents)) - 1])

        self.chat = type("Chat", (), {"completions": _Completions()})()


def _json(sql: str) -> str:
    return json.dumps({"sql": sql, "explanation": "x"})


BROKEN_FUNCTION_SQL = "SELECT nosuchfunc(customer_name) AS x FROM customers"
GOOD_SQL = "SELECT customer_name FROM customers ORDER BY revenue DESC"


def test_execution_error_is_fed_back_cleaned(demo_db_url: str) -> None:
    generator = ScriptedGenerator(BROKEN_FUNCTION_SQL, GOOD_SQL)
    qp = QueryPilot.connect(demo_db_url, generator=generator, max_rows=2)

    answer = qp.ask("Top customers")

    assert answer.rows[0]["customer_name"] == "Acme Corp"
    assert len(generator.repairs) == 1
    previous_sql, feedback = generator.repairs[0]
    assert previous_sql == BROKEN_FUNCTION_SQL
    assert feedback.valid is False
    assert feedback.errors[0].lower() == "database error: no such function: nosuchfunc"
    joined = "\n".join(feedback.errors)
    assert "[SQL:" not in joined
    assert "Background on this error" not in joined
    assert "sqlite3.OperationalError" not in joined


def test_llm_repair_prompt_contains_execution_feedback(demo_db_url: str) -> None:
    client = FakeChatClient([_json(BROKEN_FUNCTION_SQL), _json(GOOD_SQL)])
    generator = OpenAICompatibleSQLGenerator(client=client, model="local")
    qp = QueryPilot.connect(demo_db_url, generator=generator, max_rows=2)

    answer = qp.ask("Top customers")

    assert answer.sql.endswith("LIMIT 2")
    assert len(client.calls) == 2
    second_prompt = client.calls[1]["messages"][1]["content"]
    assert "The previous query may need changes" in second_prompt
    assert "no such function" in second_prompt.lower()
    assert f"Previous SQL:\n{BROKEN_FUNCTION_SQL}" in second_prompt
    assert "If the previous query is already correct, return it unchanged" in second_prompt
    # Gentle wording only: no "your query is wrong/buggy" framing.
    assert "buggy" not in second_prompt.lower()
    assert "wrong" not in second_prompt.lower()


def test_empty_result_gets_one_review_with_samples_in_context(demo_db_url: str) -> None:
    generator = ScriptedGenerator(
        "SELECT revenue FROM invoices WHERE month = '2026-4'",
        "SELECT revenue FROM invoices WHERE month = '2026-04'",
    )
    qp = QueryPilot.connect(demo_db_url, generator=generator)

    answer = qp.ask("Invoice revenue in April 2026")

    assert answer.rows == [{"revenue": 9000}, {"revenue": 8000}]
    assert len(generator.repairs) == 1
    assert generator.repairs[0][1].errors == [EMPTY_RESULT_FEEDBACK]
    invoices = generator.schemas[1].get_table("invoices")
    assert invoices is not None
    assert "2026-04" in invoices.get_column("month").sample_values


def test_same_sql_after_empty_review_accepts_empty_result(demo_db_url: str) -> None:
    generator = ScriptedGenerator(
        "SELECT revenue FROM customers WHERE revenue > 1000000",
        "select revenue\nfrom customers   where revenue > 1000000;",
        "SELECT 1 AS should_not_be_reached FROM customers",
    )
    qp = QueryPilot.connect(demo_db_url, generator=generator, max_generation_attempts=3)

    answer = qp.ask("Customers above a million")

    assert answer.rows == []
    assert len(generator.repairs) == 1
    assert "1000000" in answer.sql


def test_empty_result_kept_when_review_breaks_the_query(demo_db_url: str) -> None:
    generator = ScriptedGenerator(
        "SELECT revenue FROM customers WHERE revenue > 1000000",
        "SELECT no_such_column FROM customers",
    )
    qp = QueryPilot.connect(demo_db_url, generator=generator, max_generation_attempts=3)

    answer = qp.ask("Customers above a million")

    assert answer.rows == []
    assert "1000000" in answer.sql
    assert len(generator.repairs) == 2


def test_empty_result_accepted_without_attempts_left(demo_db_url: str) -> None:
    generator = ScriptedGenerator("SELECT revenue FROM customers WHERE revenue < 0")
    qp = QueryPilot.connect(demo_db_url, generator=generator, max_generation_attempts=1)

    assert qp.ask("Negative revenue").rows == []
    assert generator.repairs == []


def test_validation_feedback_includes_did_you_mean(demo_db_url: str) -> None:
    generator = ScriptedGenerator("SELECT customer_nme FROM customers", GOOD_SQL)
    qp = QueryPilot.connect(demo_db_url, generator=generator)

    qp.ask("Customer names")

    errors = generator.repairs[0][1].errors
    assert errors[0] == "Unknown column: customer_nme"
    assert any(
        line.startswith("Hint:") and "did you mean customers.customer_name" in line
        for line in errors
    )


def test_restricted_column_hint_and_no_suggestion_of_hidden_columns(demo_db_url: str) -> None:
    policy = AccessPolicy(blocked_columns={"customers": ["revenue"]})
    generator = ScriptedGenerator(
        "SELECT revenu FROM customers",
        "SELECT revenue FROM customers",
        "SELECT customer_name FROM customers",
    )
    qp = QueryPilot.connect(demo_db_url, generator=generator, access_policy=policy)

    answer = qp.ask("Customer revenue")

    assert answer.rows
    first, second = (feedback.errors for _, feedback in generator.repairs)
    assert not any("customers.revenue" in line for line in first)
    # The blocked column is not named, but the model is told to leave it out.
    assert not any("revenue" in line for line in second)
    assert any("restricted by the access policy" in line for line in second)
    # The blocked column never reaches the model's schema.
    assert all(
        schema.get_table("customers").get_column("revenue") is None
        for schema in generator.schemas
    )


def test_final_failure_raises_last_cleaned_error(demo_db_url: str) -> None:
    generator = ScriptedGenerator("SELECT nope FROM customers")
    qp = QueryPilot.connect(demo_db_url, generator=generator, max_generation_attempts=3)

    with pytest.raises(ValueError, match="SQL validation failed: Unknown column: nope"):
        qp.ask("anything")
    assert len(generator.repairs) == 2
    assert qp.get_audit_records(1)[0].operation == "ask"
    assert qp.get_audit_records(1)[0].executed is False


def test_generator_without_repair_reports_execution_error(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, generator=GenerateOnly(BROKEN_FUNCTION_SQL))

    with pytest.raises(ValueError, match="SQL execution failed: no such function"):
        qp.ask("anything")


def test_generator_without_repair_still_answers(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, generator=GenerateOnly(GOOD_SQL), max_rows=1)

    assert qp.ask("anything").rows == [{"customer_name": "Acme Corp"}]


def test_repair_returning_no_sql_reports_previous_error(demo_db_url: str) -> None:
    generator = ScriptedGenerator("SELECT nope FROM customers", None)
    qp = QueryPilot.connect(demo_db_url, generator=generator)

    with pytest.raises(ValueError, match="Unknown column: nope"):
        qp.ask("anything")


def test_repair_feedback_and_errors_redact_stored_values(tmp_path) -> None:
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("duckdb_engine")
    path = tmp_path / "users.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE users (id INTEGER, email VARCHAR); INSERT INTO users VALUES (1, 'alice@x.com');")
    conn.close()
    policy = AccessPolicy(masking_rules={"users": {"email": {"mode": "redact"}}})
    generator = ScriptedGenerator("SELECT CAST(email AS INTEGER) AS n FROM users")
    qp = QueryPilot.connect(f"duckdb:///{path}", generator=generator, access_policy=policy)

    with pytest.raises(ValueError) as raised:
        qp.ask("email as a number")

    feedback = [line for _, validation in generator.repairs for line in validation.errors]
    assert feedback and all("alice" not in line for line in feedback)
    assert any("<redacted>" in line for line in feedback)
    assert "alice" not in str(raised.value)
    # The repair request carries no rewritten SQL or policy details.
    assert all(validation.rewritten_sql is None for _, validation in generator.repairs)
    # Operators still get the full database error.
    assert "alice@x.com" in (qp.get_audit_records(1)[0].error or "")


def test_ask_errors_do_not_name_hidden_columns(demo_db_url: str) -> None:
    policy = AccessPolicy(blocked_columns={"customers": ["revenue"]})
    generator = ScriptedGenerator("SELECT * FROM customers")
    qp = QueryPilot.connect(demo_db_url, generator=generator, access_policy=policy)

    with pytest.raises(ValueError) as raised:
        qp.ask("everything about customers")

    assert "revenue" not in str(raised.value)
    assert "restricted by the access policy" in str(raised.value)
    assert all(
        "revenue" not in line for _, validation in generator.repairs for line in validation.errors
    )
    assert "customers.revenue" in (qp.get_audit_records(1)[0].error or "")


def test_generation_failure_on_pruned_schema_retries_with_more_context(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "wide.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, segment TEXT)")
    conn.execute("INSERT INTO customers VALUES (1, 'smb')")
    for index in range(40):
        conn.execute(f"CREATE TABLE other_{index} (id INTEGER PRIMARY KEY, note_{index} TEXT, value_{index} REAL)")
    conn.commit()
    conn.close()
    generator = ScriptedGenerator(None, "SELECT segment FROM customers")
    qp = QueryPilot.connect(f"sqlite:///{path}", generator=generator, schema_token_budget=120, sample_values=0)

    answer = qp.ask("which segments exist")

    assert answer.rows == [{"segment": "smb"}]
    first, second = generator.schemas
    assert len(second.tables) > len(first.tables)
