from __future__ import annotations

import pytest

from querypilot import QueryPilot
from querypilot.core.config import QueryPilotConfig, SafetyPolicy
from querypilot.core.types import DatabaseSchema
from querypilot.validation.validator import SQLValidator


def test_select_is_rewritten_with_limit(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2)

    result = qp.validate_sql("SELECT * FROM customers")

    assert result.valid is True
    assert result.readonly is True
    assert result.limit_applied is True
    assert result.rewritten_sql == "SELECT * FROM customers LIMIT 2"
    assert result.tables == ["customers"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "UPDATE customers SET arr = 0",
        "INSERT INTO customers (customer_name, arr, revenue) VALUES ('x', 1, 1)",
        "DELETE FROM customers",
        "CREATE TABLE unsafe (id INTEGER)",
        "ALTER TABLE customers ADD COLUMN unsafe TEXT",
        "TRUNCATE TABLE customers",
        "GRANT SELECT ON customers TO analyst",
        "REVOKE SELECT ON customers FROM analyst",
        "COPY customers TO STDOUT",
    ],
)
def test_dangerous_sql_is_rejected(demo_db_url: str, sql: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql(sql)

    assert result.valid is False
    assert result.readonly is False
    assert result.errors


def test_unknown_table_is_rejected(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql("SELECT * FROM payments")

    assert result.valid is False
    assert "Unknown table: payments" in result.errors


def test_blocked_table_is_rejected(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", blocked_tables=["invoices"])

    result = qp.validate_sql("SELECT * FROM invoices")

    assert result.valid is False
    assert "Blocked table referenced: invoices" in result.errors


def test_allowed_table_policy_is_enforced(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", allowed_tables=["customers"])

    result = qp.validate_sql("SELECT * FROM invoices")

    assert result.valid is False
    assert "Table is not in allowed_tables: invoices" in result.errors


def test_existing_limit_is_preserved_or_capped(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=10)

    small = qp.validate_sql("SELECT * FROM customers LIMIT 3")
    large = qp.validate_sql("SELECT * FROM customers LIMIT 500")

    assert small.rewritten_sql == "SELECT * FROM customers LIMIT 3"
    assert small.limit_applied is False
    assert large.rewritten_sql == "SELECT * FROM customers LIMIT 10"
    assert large.limit_applied is True


def test_validation_includes_policy_checks_risk_and_fingerprint(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=10)

    result = qp.validate_sql("SELECT customer_name FROM customers")

    assert result.risk_level == "low"
    assert result.query_fingerprint
    assert {check.name for check in result.policy_checks} >= {
        "parseable",
        "single_statement",
        "readonly_select",
        "known_tables",
        "row_limit",
    }
    assert all(check.passed for check in result.policy_checks)


def test_multi_statement_sql_is_rejected(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql("SELECT * FROM customers; SELECT * FROM invoices")

    assert result.valid is False
    assert result.risk_level == "critical"
    assert result.blocked_reason == "Multiple SQL statements are not allowed."
    assert any(check.name == "single_statement" and not check.passed for check in result.policy_checks)


def test_malformed_sql_returns_structured_validation_failure(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql("SELECT FROM")

    assert result.valid is False
    assert result.risk_level == "critical"
    assert result.blocked_reason is not None
    assert result.blocked_reason.startswith("SQL parse error:")
    assert any(check.name == "parseable" and not check.passed for check in result.policy_checks)


def test_select_star_warns_by_default_and_can_be_rejected(demo_db_url: str) -> None:
    permissive = QueryPilot.connect(demo_db_url, dialect="sqlite")
    strict = QueryPilot.connect(
        demo_db_url,
        dialect="sqlite",
        safety_policy=SafetyPolicy(allow_select_star=False),
    )

    permissive_result = permissive.validate_sql("SELECT * FROM customers")
    strict_result = strict.validate_sql("SELECT * FROM customers")

    assert permissive_result.valid is True
    assert "SELECT * may expose more data than intended." in permissive_result.warnings
    assert permissive_result.risk_level == "medium"
    assert strict_result.valid is False
    assert strict_result.blocked_reason == "SELECT * is disabled by policy."


def test_cartesian_join_is_rejected(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql("SELECT customers.id, invoices.id FROM customers, invoices")

    assert result.valid is False
    assert result.risk_level == "high"
    assert result.blocked_reason == "Potential Cartesian join detected."
    assert any(check.name == "join_safety" and not check.passed for check in result.policy_checks)


def test_dangerous_word_inside_literal_or_comment_is_allowed(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    literal = qp.validate_sql("SELECT 'drop' AS harmless FROM customers")
    comment = qp.validate_sql("SELECT customer_name FROM customers -- drop is documentation")

    assert literal.valid is True
    assert comment.valid is True


def test_nested_write_expression_is_rejected(demo_db_url: str) -> None:
    schema = QueryPilot.connect(demo_db_url, dialect="sqlite").get_schema()
    validator = SQLValidator(QueryPilotConfig(dialect="postgres"))

    result = validator.validate(
        "WITH changed AS (DELETE FROM customers RETURNING id) SELECT * FROM changed",
        schema,
    )

    assert result.valid is False
    assert result.blocked_reason == "SQL contains a non-read-only operation: DELETE"


@pytest.mark.parametrize(
    "function_name",
    [
        "nextval",
        "setval",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_sleep",
    ],
)
def test_dangerous_postgres_functions_are_rejected(function_name: str) -> None:
    validator = SQLValidator(QueryPilotConfig(dialect="postgres"))

    result = validator.validate(
        f"SELECT {function_name}('x')",
        DatabaseSchema(dialect="postgres"),
    )

    assert result.valid is False
    assert result.blocked_reason == f"SQL function is blocked by policy: {function_name}"


def test_function_allowlist_fails_closed() -> None:
    validator = SQLValidator(
        QueryPilotConfig(
            dialect="postgres",
            safety_policy=SafetyPolicy(allowed_functions=["count"]),
        )
    )
    schema = DatabaseSchema(dialect="postgres")

    assert validator.validate("SELECT COUNT(*)", schema).valid is True
    result = validator.validate("SELECT LOWER('A')", schema)

    assert result.valid is False
    assert result.blocked_reason == "SQL function is not allowed by policy: lower"
