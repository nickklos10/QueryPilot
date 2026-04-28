from __future__ import annotations

import pytest

from querypilot import QueryPilot


def test_sqlite_schema_introspection(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    schema = qp.get_schema()

    assert sorted(table.name for table in schema.tables) == ["customers", "invoices"]
    customers = schema.get_table("customers")
    assert customers is not None
    assert [column.name for column in customers.columns] == [
        "id",
        "customer_name",
        "arr",
        "revenue",
    ]


def test_safe_query_execution(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2)

    result = qp.execute_sql("SELECT customer_name, arr FROM customers ORDER BY arr DESC")

    assert result.sql == "SELECT customer_name, arr FROM customers ORDER BY arr DESC LIMIT 2"
    assert result.row_count == 2
    assert result.rows[0] == {"customer_name": "Acme Corp", "arr": 120000}


def test_dangerous_sql_is_not_executed(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    with pytest.raises(ValueError, match="SQL validation failed"):
        qp.execute_sql("DROP TABLE customers")

    schema = qp.get_schema()
    assert schema.get_table("customers") is not None
