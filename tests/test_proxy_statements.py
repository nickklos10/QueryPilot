from __future__ import annotations

import pytest

pytest.importorskip("pygwire")

from querypilot.access import AccessPolicy
from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import ColumnSchema, DatabaseSchema, TableSchema
from querypilot.proxy.statements import StatementPolicy, split_statements


def _policy(**kwargs) -> StatementPolicy:
    schema = DatabaseSchema(
        dialect="postgres",
        tables=[
            TableSchema(
                name="customers",
                columns=[ColumnSchema(name=name, type="text") for name in ("id", "name", "email", "tenant_id")],
            ),
            TableSchema(
                name="orders",
                columns=[ColumnSchema(name=name, type="int") for name in ("id", "customer_id", "amount", "tenant_id")],
            ),
        ],
    )
    config = QueryPilotConfig(
        dialect="postgres",
        max_rows=50,
        access_policy=AccessPolicy(
            row_filters={"orders": "tenant_id = 1"},
            masking_rules={"customers": {"email": {"mode": "redact"}}},
        ),
    )
    return StatementPolicy(
        config,
        schema,
        catalog_relations={"pg_class", "pg_type", "pg_namespace", "pg_stats", "pg_attribute"},
        data_schemas={"public"},
        **kwargs,
    )


@pytest.mark.parametrize(
    ("sql", "upstream"),
    [
        ("SET extra_float_digits = 3", "SET extra_float_digits TO 3"),
        ("set client_encoding to 'UTF8'", "SET client_encoding TO 'UTF8'"),
        ("SET TIME ZONE 'UTC'", "SET timezone TO 'UTC'"),
        ("SET LOCAL datestyle = ISO, MDY", "SET LOCAL datestyle TO 'ISO', 'MDY'"),
        ("SET application_name = 'x'' ; DROP TABLE t; --'", "SET application_name TO 'x'' ; DROP TABLE t; --'"),
        ("RESET application_name", "RESET application_name"),
        ("show transaction isolation level", "SHOW transaction_isolation"),
        ("BEGIN", "BEGIN READ ONLY"),
        ("START TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY", "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"),
        ("COMMIT", "COMMIT"),
        ("ROLLBACK TO SAVEPOINT s1", 'ROLLBACK TO SAVEPOINT "s1"'),
    ],
)
def test_session_commands_are_regenerated_not_forwarded(sql: str, upstream: str) -> None:
    statement = _policy().classify(sql)

    assert statement.kind != "rejected", statement.message
    assert statement.upstream_sql == upstream


@pytest.mark.parametrize(
    "sql",
    [
        "SET search_path = other",
        "SET default_transaction_read_only = off",
        "SET SESSION AUTHORIZATION postgres",
        "SET ROLE postgres",
        "SET statement_timeout = 0",
        "SET client_encoding = 'LATIN1'",
        "SET standard_conforming_strings = off",
        "SET datestyle = (SELECT email FROM customers)",
        "BEGIN READ WRITE",
        "SET TRANSACTION READ WRITE",
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
        "RESET ALL",
        "SHOW ALL",
        "SHOW data_directory",
        "COPY orders TO STDOUT",
        "PREPARE p AS SELECT email FROM customers",
        "EXPLAIN ANALYZE SELECT 1",
        "DISCARD ALL",
        "DO $$ BEGIN DELETE FROM orders; END $$",
    ],
)
def test_unlisted_session_commands_fail_closed(sql: str) -> None:
    assert _policy().classify(sql).kind == "rejected"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE orders SET amount = 0",
        "INSERT INTO orders VALUES (1, 1, 1, 1)",
        "DROP TABLE orders",
        "TRUNCATE orders",
        "CREATE TABLE x (id int)",
        "SELECT * INTO copy_of_orders FROM orders",
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        "SELECT * FROM other.orders",
        "SELECT * FROM otherdb.public.orders",
    ],
)
def test_writes_and_escapes_are_rejected(sql: str) -> None:
    statement = _policy().classify(sql)

    assert statement.kind == "rejected"
    assert statement.upstream_sql is None


def test_query_is_rewritten_with_row_filter_mask_and_limit() -> None:
    statement = _policy().classify(
        "SELECT c.email, o.amount FROM orders o JOIN customers c ON c.id = o.customer_id"
    )

    assert statement.kind == "query"
    assert "tenant_id = 1" in statement.upstream_sql
    assert statement.upstream_sql.endswith("LIMIT 50")
    assert statement.output_masks == ["redact", None]


def test_rejection_carries_sqlstate_and_agent_message() -> None:
    statement = _policy().classify("SELECT nope FROM orders")

    assert statement.kind == "rejected"
    assert statement.sqlstate == "42703"
    assert "nope" in statement.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT version()",
        "select pg_catalog.version()",
        "SELECT oid, typarray FROM pg_type WHERE typname = $1",
        (
            "SELECT c.relname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE pg_catalog.pg_table_is_visible(c.oid)"
        ),
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    ],
)
def test_catalog_queries_pass_through_unchanged(sql: str) -> None:
    statement = _policy().classify(sql)

    assert statement.kind == "catalog"
    assert statement.upstream_sql == sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_stats",
        "SELECT query FROM pg_catalog.pg_stat_activity",
        "SELECT query_to_xml('select email from customers', true, true, '') FROM pg_class",
        "SELECT public.version() FROM pg_class",
        "SELECT ts_stat('select email from customers') FROM pg_class",
        "SELECT c.relname, (SELECT email FROM customers LIMIT 1) FROM pg_class c",
    ],
)
def test_catalog_queries_cannot_reach_data(sql: str) -> None:
    assert _policy().classify(sql).kind == "rejected"


def test_catalog_queries_can_be_disabled() -> None:
    statement = _policy(allow_catalog=False).classify("SELECT relname FROM pg_class")

    assert statement.kind == "rejected"


def test_split_statements_keeps_semicolons_inside_literals() -> None:
    assert split_statements("SELECT ';' AS a; SELECT 2;") == ["SELECT ';' AS a", " SELECT 2"]
    assert split_statements("  ") == ["  "]
