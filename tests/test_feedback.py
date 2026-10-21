"""Repair hints and database-error cleaning."""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from querypilot.core.types import ColumnSchema, DatabaseSchema, ForeignKey, TableSchema
from querypilot.validation.feedback import (
    INVALID_ROW_FILTER_ERROR,
    clean_error,
    describe_db_error,
    public_errors,
    redact_values,
    repair_hints,
    restricted_error,
)


def _schema() -> DatabaseSchema:
    def cols(*names: str) -> list[ColumnSchema]:
        return [ColumnSchema(name=name, type="TEXT") for name in names]

    return DatabaseSchema(
        dialect="sqlite",
        tables=[
            TableSchema(name="customers", columns=cols("id", "customer_name", "segment")),
            TableSchema(
                name="orders",
                columns=cols("id", "customer_id", "amount", "status"),
                foreign_keys=[
                    ForeignKey(columns=["customer_id"], referred_table="customers", referred_columns=["id"])
                ],
            ),
        ],
    )


def test_clean_error_strips_ansi() -> None:
    assert clean_error("\x1b[31mUnknown column: x\x1b[0m  ") == "Unknown column: x"


def test_unknown_column_did_you_mean_and_table_columns() -> None:
    hints = repair_hints(["Unknown column: custmer_name"], _schema(), "SELECT custmer_name FROM customers")

    assert len(hints) == 1
    assert "did you mean customers.customer_name?" in hints[0]
    assert "Columns of customers: id, customer_name, segment." in hints[0]


def test_unknown_column_resolves_alias_to_table() -> None:
    hints = repair_hints(
        ["Unknown column: stat"],
        _schema(),
        "SELECT o.stat FROM orders o JOIN customers c ON o.customer_id = c.id",
    )

    assert "did you mean orders.status?" in hints[0]
    assert "Columns of orders:" in hints[0]


def test_unknown_column_without_sql_uses_all_columns() -> None:
    hints = repair_hints(["Unknown column: amount_total"], _schema())

    assert "orders.amount" in hints[0]


def test_unknown_table_did_you_mean_and_table_list() -> None:
    hints = repair_hints(["Unknown table: order"], _schema())

    assert hints == [
        "Table 'order' does not exist; did you mean orders? Available tables: customers, orders."
    ]


def test_unknown_table_list_is_truncated_for_huge_schemas() -> None:
    schema = DatabaseSchema(
        dialect="sqlite", tables=[TableSchema(name=f"t{i:03d}") for i in range(100)]
    )

    hint = repair_hints(["Unknown table: zzz"], schema)[0]

    assert "(70 more)" in hint


def test_ambiguous_column_names_owning_tables() -> None:
    hints = repair_hints(
        ["Ambiguous column: id"],
        _schema(),
        "SELECT id FROM orders JOIN customers ON orders.customer_id = customers.id",
    )

    assert hints == ["'id' exists in customers, orders; qualify it, e.g. customers.id, orders.id."]


@pytest.mark.parametrize(
    "error",
    [
        "Column is blocked by access policy: customers.segment",
        "Column is not allowed by access policy: customers.segment",
    ],
)
def test_restricted_column_hint_does_not_name_the_column(error: str) -> None:
    hint = repair_hints([error], _schema())[0]

    assert hint.startswith("A column the query uses is restricted")
    assert "segment" not in hint


@pytest.mark.parametrize(
    "error", ["Blocked table referenced: payroll", "Table is not in allowed_tables: payroll"]
)
def test_restricted_table_hint_does_not_name_the_table(error: str) -> None:
    hint = repair_hints([error], _schema())[0]

    assert hint.startswith("A table the query uses is restricted")
    assert "payroll" not in hint


def test_cartesian_join_hint_lists_foreign_keys() -> None:
    hints = repair_hints(
        ["Potential Cartesian join detected."], _schema(), "SELECT * FROM orders, customers"
    )

    assert hints == [
        "Add a join condition using the foreign keys: orders.customer_id = customers.id."
    ]


def test_unrelated_errors_get_no_hint_and_hints_are_deduplicated() -> None:
    errors = ["Unknown table: order", "Unknown table: order", "something else entirely"]

    assert len(repair_hints(errors, _schema())) == 1


def test_describe_db_error_cleans_sqlalchemy_message(tmp_path) -> None:
    db = tmp_path / "x.db"
    sqlite3.connect(db).executescript("CREATE TABLE customers (id INTEGER, segment TEXT);")
    engine = create_engine(f"sqlite:///{db}")
    sql = "SELECT segmnt\nFROM customers"
    with pytest.raises(OperationalError) as info, engine.connect() as conn:
        conn.execute(text(sql))

    lines = describe_db_error(info.value, _schema(), sql)

    assert lines[0] == "Database error: no such column: segmnt"
    assert "did you mean customers.segment?" in lines[1]


class _PgError(Exception):
    pass


def test_describe_db_error_drops_postgres_echo_lines() -> None:
    exc = _PgError(
        'column "segmnt" does not exist\nLINE 1: SELECT segmnt FROM customers\n'
        "               ^\nHINT:  Perhaps you meant to reference the column \"customers.segment\"."
    )

    lines = describe_db_error(exc, _schema())

    assert lines[0].startswith('Database error: column "segmnt" does not exist HINT:')
    assert "LINE 1" not in lines[0]
    assert "did you mean customers.segment?" in lines[1]


def test_describe_db_error_handles_unknown_table() -> None:
    lines = describe_db_error(_PgError('relation "order" does not exist'), _schema())

    assert "did you mean orders?" in lines[1]


def test_describe_db_error_without_known_pattern_is_just_the_message() -> None:
    assert describe_db_error(_PgError("division by zero"), _schema()) == [
        "Database error: division by zero"
    ]


@pytest.mark.parametrize(
    "message",
    ["interrupted", "canceling statement due to statement timeout"],
)
def test_describe_db_error_adds_timeout_hint(message: str) -> None:
    lines = describe_db_error(_PgError(message), _schema())

    assert lines[1].startswith("The query took too long")


# --------------------------------------------------------------------------- leaks


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            'invalid input syntax for type integer: "alice@x.com"',
            'invalid input syntax for type integer: "<redacted>"',
        ),
        (
            "Conversion Error: Could not convert string 'alice@x.com' to INT32",
            "Conversion Error: Could not convert string '<redacted>' to INT32",
        ),
        # Values may contain quotes, and apostrophes inside words are not quotes.
        ("Truncated incorrect INTEGER value: 'O'Brien'", "Truncated incorrect INTEGER value: '<redacted>'"),
        ("can't convert 'bob' now", "can't convert '<redacted>' now"),
        ("could not convert string 'cut off", "could not convert string '<redacted>'"),
        # Schema names (also qualified) and the query's own tokens are kept.
        ('column "customers.segment" must appear in GROUP BY', 'column "customers.segment" must appear in GROUP BY'),
        ("Table 'shop.customers' doesn't exist", "Table 'shop.customers' doesn't exist"),
        ('near "SELEC": syntax error', 'near "SELEC": syntax error'),
    ],
)
def test_redact_values_keeps_identifiers_and_hides_stored_values(message: str, expected: str) -> None:
    assert redact_values(message, _schema(), "SELEC segment FROM customers") == expected


def test_describe_db_error_redacts_values_but_keeps_the_unknown_name() -> None:
    value = describe_db_error(_PgError('invalid input syntax for type integer: "alice@x.com"'), _schema())
    unknown = describe_db_error(_PgError('column "segmnt" does not exist'), _schema())

    assert value == ['Database error: invalid input syntax for type integer: "<redacted>"']
    assert unknown[0] == 'Database error: column "segmnt" does not exist'
    assert "did you mean customers.segment?" in unknown[1]


def test_public_errors_hide_restricted_names_and_row_filters() -> None:
    errors = [
        "Column is blocked by access policy: customers.revenue",
        "Column is not allowed by access policy: customers.notes",
        "Blocked table referenced: payroll",
        "Invalid row filter for orders: Invalid expression / Unexpected token. tenant_id = = 42",
        "Unknown column: segmnt",
    ]

    public = public_errors(errors, "describe_table")

    assert public == [
        restricted_error("describe_table"),
        INVALID_ROW_FILTER_ERROR,
        "Unknown column: segmnt",
    ]
    assert "restricted by the access policy" in public[0] and "describe_table" in public[0]
    assert not any(name in " ".join(public) for name in ("revenue", "notes", "payroll", "tenant_id"))
