from __future__ import annotations

from types import SimpleNamespace

from querypilot.connectors.postgres import PostgresConnector, _normalize_postgres_url


class _FakeResult:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []

    def close(self) -> None:
        return None

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, executed: list[str]) -> None:
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, statement):
        sql = str(statement)
        self.executed.append(sql)
        if sql == "SELECT 1 AS value":
            return _FakeResult([SimpleNamespace(_mapping={"value": 1})])
        return _FakeResult()


class _FakeEngine:
    def __init__(self, executed: list[str]) -> None:
        self.connection = _FakeConnection(executed)

    def connect(self):
        return self.connection


def test_postgres_url_defaults_to_psycopg_driver() -> None:
    assert (
        _normalize_postgres_url("postgresql://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )


def test_execute_readonly_sets_transaction_before_timeout_and_query() -> None:
    executed: list[str] = []
    connector = object.__new__(PostgresConnector)
    connector.timeout_seconds = 10
    connector.engine = _FakeEngine(executed)

    rows, row_count = connector.execute_readonly("SELECT 1 AS value")

    assert executed == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = 10000",
        "SELECT 1 AS value",
    ]
    assert rows == [{"value": 1}]
    assert row_count == 1
    assert (
        _normalize_postgres_url("postgres://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )
    assert (
        _normalize_postgres_url("postgresql+psycopg://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )
