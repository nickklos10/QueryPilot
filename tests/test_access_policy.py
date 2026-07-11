from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from querypilot import QueryPilot
from querypilot.access import AccessPolicy, MaskingRule


@pytest.fixture()
def tenant_db_url(tmp_path: Path) -> str:
    db_path = tmp_path / "tenant.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            email TEXT NOT NULL,
            revenue INTEGER NOT NULL
        );

        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            amount INTEGER NOT NULL
        );

        INSERT INTO customers (tenant_id, customer_name, email, revenue) VALUES
            (42, 'Acme Corp', 'ceo@acme.example', 120000),
            (42, 'Globex', 'finance@globex.example', 95000),
            (7, 'OtherCo', 'owner@other.example', 500000);

        INSERT INTO orders (customer_id, amount) VALUES (1, 1000);
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db_path}"


def test_blocked_column_policy_rejects_query(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        dialect="sqlite",
        access_policy=AccessPolicy(blocked_columns={"customers": ["email"]}),
    )

    result = qp.validate_sql("SELECT email FROM customers")

    assert result.valid is False
    assert result.blocked_reason == "Column is blocked by access policy: customers.email"
    assert "customers.email" in result.access_policy["blocked_columns"]


def test_allowed_column_policy_rejects_columns_outside_allowlist(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        dialect="sqlite",
        access_policy=AccessPolicy(allowed_columns={"customers": ["customer_name"]}),
    )

    result = qp.validate_sql("SELECT customer_name, revenue FROM customers")

    assert result.valid is False
    assert result.blocked_reason == "Column is not allowed by access policy: customers.revenue"


def test_required_row_filter_is_injected_before_execution(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        dialect="sqlite",
        max_rows=10,
        access_policy=AccessPolicy(row_filters={"customers": "tenant_id = 42"}),
    )

    result = qp.execute_sql("SELECT customer_name FROM customers ORDER BY customer_name")

    assert result.sql == (
        "SELECT customer_name FROM customers "
        "WHERE tenant_id = 42 ORDER BY customer_name LIMIT 10"
    )
    assert [row["customer_name"] for row in result.rows] == ["Acme Corp", "Globex"]
    assert result.access_policy["row_filters"] == {"customers": "tenant_id = 42"}


def test_required_row_filter_is_combined_with_existing_where(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        dialect="sqlite",
        max_rows=10,
        access_policy=AccessPolicy(row_filters={"customers": "tenant_id = 42"}),
    )

    result = qp.validate_sql("SELECT customer_name FROM customers WHERE revenue > 100000")

    assert result.valid is True
    assert result.rewritten_sql == (
        "SELECT customer_name FROM customers "
        "WHERE revenue > 100000 AND tenant_id = 42 LIMIT 10"
    )


def test_masking_policy_redacts_returned_rows_and_is_audited(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        dialect="sqlite",
        access_policy=AccessPolicy(
            row_filters={"customers": "tenant_id = 42"},
            masking_rules={
                "customers": {
                    "email": MaskingRule(mode="redact"),
                }
            },
        ),
    )

    result = qp.execute_sql("SELECT customer_name, email FROM customers ORDER BY customer_name")
    record = qp.get_audit_records()[0]

    assert result.rows[0]["email"] == "[REDACTED]"
    assert result.rows[1]["email"] == "[REDACTED]"
    assert result.access_policy["masked_columns"] == {"customers.email": "redact"}
    assert record.access_policy == result.access_policy


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",
        "SELECT c.email FROM customers AS c",
        (
            "SELECT email FROM customers "
            "JOIN orders ON customers.id = orders.customer_id"
        ),
    ],
)
def test_blocked_columns_cannot_be_bypassed(tenant_db_url: str, sql: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        access_policy=AccessPolicy(blocked_columns={"customers": ["email"]}),
    )

    result = qp.validate_sql(sql)

    assert result.valid is False
    assert result.blocked_reason == "Column is blocked by access policy: customers.email"


def test_allowed_columns_apply_through_alias_and_star(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        access_policy=AccessPolicy(allowed_columns={"customers": ["customer_name"]}),
    )

    alias = qp.validate_sql("SELECT c.revenue FROM customers AS c")
    star = qp.validate_sql("SELECT c.* FROM customers AS c")

    assert alias.blocked_reason == "Column is not allowed by access policy: customers.revenue"
    assert star.valid is False
    assert star.blocked_reason is not None
    assert star.blocked_reason.startswith(
        "Column is not allowed by access policy: customers."
    )


def test_multitable_columns_fail_closed(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(tenant_db_url)

    unknown = qp.validate_sql(
        "SELECT missing FROM customers "
        "JOIN orders ON customers.id = orders.customer_id"
    )
    ambiguous = qp.validate_sql(
        "SELECT id FROM customers JOIN orders ON customers.id = orders.customer_id"
    )
    qualified = qp.validate_sql(
        "SELECT customers.id, orders.amount FROM customers "
        "JOIN orders ON customers.id = orders.customer_id"
    )

    assert unknown.blocked_reason == "Unknown column: missing"
    assert ambiguous.blocked_reason == "Ambiguous column: id"
    assert qualified.valid is True
