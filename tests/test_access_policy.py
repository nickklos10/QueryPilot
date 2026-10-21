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
        "SELECT customer_name FROM (SELECT * FROM customers WHERE tenant_id = 42) AS customers "
        "ORDER BY customer_name LIMIT 10"
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
        "SELECT customer_name FROM (SELECT * FROM customers WHERE tenant_id = 42) AS customers "
        "WHERE revenue > 100000 LIMIT 10"
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


ROW_FILTER = AccessPolicy(row_filters={"customers": "tenant_id = 42"})


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT (SELECT COUNT(*) FROM customers) AS n FROM customers LIMIT 1",
            [{"n": 2}],
        ),
        (
            "SELECT COUNT(*) AS n FROM (SELECT id, 42 AS tenant_id FROM customers)",
            [{"n": 2}],
        ),
        (
            "SELECT o.amount FROM orders AS o WHERE o.customer_id IN (SELECT c.id FROM customers AS c)",
            [{"amount": 1000}],
        ),
        (
            (
                "WITH everyone AS (SELECT customer_name FROM customers) "
                "SELECT COUNT(*) AS n FROM everyone"
            ),
            [{"n": 2}],
        ),
        (
            (
                "SELECT customer_name FROM customers WHERE id = 1 "
                "UNION SELECT customer_name FROM customers WHERE id = 3"
            ),
            [{"customer_name": "Acme Corp"}],
        ),
    ],
)
def test_row_filter_applies_at_every_scope(
    tenant_db_url: str, sql: str, expected: list[dict]
) -> None:
    qp = QueryPilot.connect(tenant_db_url, access_policy=ROW_FILTER)

    assert qp.execute_sql(sql).rows == expected


@pytest.fixture()
def two_tenant_tables_url(tmp_path: Path) -> str:
    db_path = tmp_path / "two_tenant.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, tenant_id INTEGER, name TEXT);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, tenant_id INTEGER, customer_id INTEGER, amount INTEGER
        );
        INSERT INTO customers VALUES (1, 42, 'Acme'), (2, 42, 'Globex'), (3, 7, 'OtherCo');
        INSERT INTO orders VALUES (1, 42, 1, 100), (2, 7, 3, 700), (3, 7, 2, 999);
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db_path}"


def _two_tenant_qp(url: str) -> QueryPilot:
    return QueryPilot.connect(
        url,
        access_policy=AccessPolicy(
            row_filters={"customers": "tenant_id = 42", "orders": "tenant_id = 42"}
        ),
    )


def test_row_filters_on_both_sides_of_a_join(two_tenant_tables_url: str) -> None:
    qp = _two_tenant_qp(two_tenant_tables_url)

    result = qp.execute_sql(
        "SELECT c.name, o.amount FROM customers AS c JOIN orders AS o ON o.customer_id = c.id"
    )

    assert result.rows == [{"name": "Acme", "amount": 100}]


def test_row_filter_preserves_left_join_semantics(two_tenant_tables_url: str) -> None:
    qp = _two_tenant_qp(two_tenant_tables_url)

    result = qp.execute_sql(
        "SELECT c.name, o.amount FROM customers AS c "
        "LEFT JOIN orders AS o ON o.customer_id = c.id ORDER BY c.name"
    )

    assert result.rows == [
        {"name": "Acme", "amount": 100},
        {"name": "Globex", "amount": None},
    ]


MASK_EMAIL = AccessPolicy(masking_rules={"customers": {"email": MaskingRule(mode="redact")}})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email AS contact FROM customers",
        "SELECT LOWER(email) AS e FROM customers",
        "SELECT 'x' || email AS e FROM customers",
        "SELECT c.email AS e FROM customers AS c",
        "SELECT (SELECT MAX(email) FROM customers) AS e FROM orders",
        "WITH x AS (SELECT email AS hidden FROM customers) SELECT hidden FROM x",
        "SELECT t.e FROM (SELECT email AS e FROM customers) AS t",
        "SELECT customer_name AS e FROM customers WHERE id = 1 UNION ALL SELECT email FROM customers",
    ],
)
def test_masking_follows_column_lineage(tenant_db_url: str, sql: str) -> None:
    qp = QueryPilot.connect(tenant_db_url, access_policy=MASK_EMAIL)

    rows = qp.execute_sql(sql).rows

    assert rows
    assert all("@" not in str(value) for row in rows for value in row.values())


def test_masking_by_position_leaves_other_columns_alone(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(tenant_db_url, access_policy=MASK_EMAIL)

    rows = qp.execute_sql("SELECT * FROM customers WHERE id = 1").rows
    counted = qp.execute_sql("SELECT COUNT(email) AS n FROM customers").rows

    assert rows == [
        {
            "id": 1,
            "tenant_id": 42,
            "customer_name": "Acme Corp",
            "email": "[REDACTED]",
            "revenue": 120000,
        }
    ]
    assert counted == [{"n": 3}]


def test_blocked_column_cannot_be_reached_through_cte_star(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        access_policy=AccessPolicy(blocked_columns={"customers": ["email"]}),
    )

    result = qp.validate_sql("WITH x AS (SELECT * FROM customers) SELECT email FROM x")

    assert result.valid is False
    assert result.blocked_reason == "Column is blocked by access policy: customers.email"
