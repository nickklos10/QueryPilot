from __future__ import annotations

import pytest

from querypilot import QueryPilot


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
