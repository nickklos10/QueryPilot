from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from querypilot import QueryPilot
from querypilot.connectors.base import BaseConnector
from querypilot.connectors.postgres import PostgresConnector
from querypilot.connectors.registry import create_connector, dialect_from_url
from querypilot.connectors.sqlalchemy import SQLAlchemyConnector, build_engine
from querypilot.connectors.sqlite import SQLiteConnector
from querypilot.core.config import QueryPilotConfig, SafetyPolicy
from querypilot.core.safety_defaults import BLOCKED_FUNCTIONS_BY_DIALECT
from querypilot.core.types import ColumnSchema, DatabaseSchema, TableSchema
from querypilot.validation.validator import SQLValidator


@pytest.mark.parametrize(
    ("url", "dialect"),
    [
        ("sqlite:///demo.db", "sqlite"),
        ("postgresql://u:p@localhost/db", "postgres"),
        ("postgresql+psycopg://u:p@localhost/db", "postgres"),
        ("postgres://u:p@localhost/db", "postgres"),
        ("duckdb:///warehouse.duckdb", "duckdb"),
        ("mysql+pymysql://u:p@localhost/db", "mysql"),
        ("mariadb://u:p@localhost/db", "mysql"),
        ("mssql+pyodbc://u:p@localhost/db", "tsql"),
        ("snowflake://u:p@account/db", "snowflake"),
        ("bigquery://project/dataset", "bigquery"),
        ("redshift+psycopg2://u:p@host:5439/db", "redshift"),
        ("oracle+oracledb://u:p@localhost/db", "oracle"),
        ("trino://u@localhost:8080/catalog", "trino"),
        ("databricks://token:x@host", "databricks"),
        ("clickhouse://u:p@localhost/db", "clickhouse"),
    ],
)
def test_dialect_inferred_from_url(url: str, dialect: str) -> None:
    assert dialect_from_url(url) == dialect


def test_unknown_url_scheme_asks_for_dialect() -> None:
    with pytest.raises(ValueError, match="Pass dialect"):
        dialect_from_url("madeupdb://localhost/db")


def test_create_connector_picks_connector_by_backend(tmp_path: Path) -> None:
    sqlite3.connect(tmp_path / "a.db").close()
    assert isinstance(create_connector(f"sqlite:///{tmp_path / 'a.db'}"), SQLiteConnector)
    assert isinstance(create_connector("postgresql://u:p@localhost/db"), PostgresConnector)
    assert isinstance(create_connector(f"duckdb:///{tmp_path / 'a.duckdb'}"), SQLAlchemyConnector)


def test_missing_driver_names_the_pip_extra() -> None:
    if _installed("snowflake.sqlalchemy"):
        pytest.skip("snowflake-sqlalchemy is installed")
    with pytest.raises(ImportError, match=r'pip install "querypilot\[snowflake\]"'):
        QueryPilot.connect("snowflake://u:p@account/db")


def test_missing_dbapi_names_the_pip_extra() -> None:
    if _installed("pyodbc"):
        pytest.skip("pyodbc is installed")
    with pytest.raises(ImportError, match=r'pip install "querypilot\[mssql\]"'):
        build_engine("mssql+pyodbc://u:p@localhost/db")


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def test_mysql_url_without_driver_uses_pymysql() -> None:
    engine = build_engine("mysql://u:p@localhost/db")
    assert engine.url.drivername == "mysql+pymysql"


# --- DuckDB end to end -------------------------------------------------------


@pytest.fixture()
def duckdb_path(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers (id),
            amount DOUBLE
        );
        CREATE VIEW large_orders AS SELECT * FROM orders WHERE amount > 100;
        COMMENT ON TABLE customers IS 'People who buy from us';
        COMMENT ON COLUMN customers.name IS 'Display name';
        CREATE SCHEMA sales;
        CREATE TABLE sales.leads (id INTEGER PRIMARY KEY);
        INSERT INTO customers VALUES (1, 'Acme'), (2, 'Globex');
        INSERT INTO orders VALUES (1, 1, 50), (2, 2, 150);
        """
    )
    conn.close()
    return path


def test_duckdb_introspection_reports_keys_views_and_comments(duckdb_path: Path) -> None:
    schema = create_connector(f"duckdb:///{duckdb_path}").get_schema()

    assert schema.dialect == "duckdb"
    assert sorted(table.name for table in schema.tables) == ["customers", "large_orders", "orders"]
    customers = schema.get_table("customers")
    assert customers.comment == "People who buy from us"
    assert customers.get_column("id").primary_key
    assert customers.get_column("name").comment == "Display name"
    assert not customers.get_column("name").nullable
    orders = schema.get_table("orders")
    assert [fk.model_dump() for fk in orders.foreign_keys] == [
        {"columns": ["customer_id"], "referred_table": "customers", "referred_columns": ["id"]}
    ]
    assert schema.get_table("large_orders").is_view
    assert not customers.is_view


def test_duckdb_introspection_of_requested_schemas(duckdb_path: Path) -> None:
    schema = create_connector(f"duckdb:///{duckdb_path}", schemas=["main", "sales"]).get_schema()

    names = {(table.schema_name, table.name) for table in schema.tables}
    assert ("sales", "leads") in names
    assert ("main", "customers") in names


def test_duckdb_execute_sql_through_querypilot(duckdb_path: Path) -> None:
    qp = QueryPilot.connect(f"duckdb:///{duckdb_path}", max_rows=5)

    result = qp.execute_sql(
        "SELECT c.name, o.amount FROM customers c JOIN orders o ON o.customer_id = c.id "
        "ORDER BY o.amount DESC"
    )

    assert qp.config.dialect == "duckdb"
    assert result.sql.endswith("LIMIT 5")
    assert result.rows == [{"name": "Globex", "amount": 150.0}, {"name": "Acme", "amount": 50.0}]


@pytest.mark.parametrize(
    "sql",
    ["CREATE TABLE x (i INTEGER)", "INSERT INTO customers VALUES (3, 'Initech')"],
)
def test_duckdb_connector_is_read_only_at_engine_level(duckdb_path: Path, sql: str) -> None:
    connector = create_connector(f"duckdb:///{duckdb_path}")

    with pytest.raises(Exception, match="read-only|read only"):
        connector.execute_readonly(sql)

    rows, _ = connector.execute_readonly("SELECT count(*) AS n FROM customers")
    assert rows == [{"n": 2}]


def test_duckdb_query_is_interrupted_at_timeout(duckdb_path: Path) -> None:
    connector = create_connector(f"duckdb:///{duckdb_path}", timeout_seconds=1)

    with pytest.raises(Exception, match="nterrupt"):
        connector.execute_readonly("SELECT count(*) FROM range(100000000000) a, range(10) b")

    assert connector.execute_readonly("SELECT 1 AS one") == ([{"one": 1}], 1)


def test_connect_accepts_a_prebuilt_engine(duckdb_path: Path) -> None:
    engine = create_engine(f"duckdb:///{duckdb_path}", connect_args={"read_only": True})

    qp = QueryPilot.connect(engine)

    assert qp.config.dialect == "duckdb"
    assert qp.execute_sql("SELECT name FROM customers ORDER BY id").rows[0] == {"name": "Acme"}


# --- SQLite ------------------------------------------------------------------


@pytest.fixture()
def sqlite_keys_url(tmp_path: Path) -> str:
    path = tmp_path / "keys.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers (id),
            amount REAL
        );
        CREATE VIEW big_orders AS SELECT * FROM orders WHERE amount > 100;
        CREATE TABLE seq (id INTEGER PRIMARY KEY AUTOINCREMENT);
        INSERT INTO customers VALUES (1, 'Acme');
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


def test_sqlite_introspection_reports_keys_and_views(sqlite_keys_url: str) -> None:
    schema = QueryPilot.connect(sqlite_keys_url).connector.get_schema()

    assert sorted(table.name for table in schema.tables) == ["big_orders", "customers", "orders", "seq"]
    assert schema.get_table("customers").get_column("id").primary_key
    assert not schema.get_table("customers").get_column("name").primary_key
    orders = schema.get_table("orders")
    assert orders.foreign_keys[0].model_dump() == {
        "columns": ["customer_id"],
        "referred_table": "customers",
        "referred_columns": ["id"],
    }
    assert schema.get_table("big_orders").is_view


def test_sqlite_engine_connector_blocks_writes(sqlite_keys_url: str) -> None:
    connector = create_connector(create_engine(sqlite_keys_url))

    assert isinstance(connector, SQLAlchemyConnector)
    with pytest.raises(Exception, match="readonly|read-only"):
        connector.execute_readonly("INSERT INTO customers VALUES (2, 'Globex')")
    assert connector.execute_readonly("SELECT count(*) AS n FROM customers") == ([{"n": 1}], 1)


# --- Session guards on engines we cannot run here -----------------------------


class _RecordingConnection:
    def __init__(self, executed: list[str]) -> None:
        self.executed = executed
        self.connection = type("Fairy", (), {"dbapi_connection": object()})()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def begin(self):
        executed = self.executed
        return type("Txn", (), {"rollback": lambda _self: executed.append("ROLLBACK")})()

    def rollback(self) -> None:
        self.executed.append("ROLLBACK")

    def execute(self, statement):
        self.executed.append(str(statement))
        return type("Result", (), {"fetchall": lambda _self: []})()


def _recording_connector(backend: str, executed: list[str]) -> SQLAlchemyConnector:
    connector = object.__new__(SQLAlchemyConnector)
    connector.timeout_seconds = 5
    connector.backend = backend
    connector.engine = type("Engine", (), {"connect": lambda _self: _RecordingConnection(executed)})()
    return connector


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("mysql", ["SET SESSION MAX_EXECUTION_TIME = 5000", "START TRANSACTION READ ONLY"]),
        ("mariadb", ["SET SESSION max_statement_time = 5", "START TRANSACTION READ ONLY"]),
        ("oracle", ["SET TRANSACTION READ ONLY"]),
        ("snowflake", ["ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 5"]),
        ("mssql", []),
    ],
)
def test_session_guards_run_before_query_and_always_roll_back(
    backend: str, expected: list[str]
) -> None:
    executed: list[str] = []

    _recording_connector(backend, executed).execute_readonly("SELECT 1")

    assert executed == [*expected, "SELECT 1", "ROLLBACK"]


def test_mariadb_behind_a_mysql_url_gets_its_own_timeout_statement() -> None:
    executed: list[str] = []
    connector = _recording_connector("mysql", executed)

    class MariaDBConnection(_RecordingConnection):
        dialect = type("Dialect", (), {"is_mariadb": True})()

    connector.engine = type("Engine", (), {"connect": lambda _self: MariaDBConnection(executed)})()
    connector.execute_readonly("SELECT 1")

    assert executed == [
        "SET SESSION max_statement_time = 5",
        "START TRANSACTION READ ONLY",
        "SELECT 1",
        "ROLLBACK",
    ]


def test_sqlite_query_only_is_restored_even_when_rollback_fails() -> None:
    executed: list[str] = []
    connector = _recording_connector("sqlite", executed)

    class FailingRollback(_RecordingConnection):
        def begin(self):
            def rollback(_self):
                raise RuntimeError("rollback failed")

            return type("Txn", (), {"rollback": rollback})()

        def execute(self, statement):
            self.executed.append(str(statement))
            return type("Result", (), {"fetchall": lambda _self: [], "scalar": lambda _self: 0})()

    connector.engine = type("Engine", (), {"connect": lambda _self: FailingRollback(executed)})()

    with pytest.raises(RuntimeError, match="rollback failed"):
        connector.execute_readonly("SELECT 1")
    assert executed[-2:] == ["PRAGMA query_only = 0", "ROLLBACK"]


def test_sqlite_engine_restores_previous_query_only_value(sqlite_keys_url: str) -> None:
    engine = create_engine(sqlite_keys_url, poolclass=StaticPool)
    event.listen(engine, "connect", lambda dbapi, _record: dbapi.execute("PRAGMA query_only = ON"))
    connector = create_connector(engine)

    assert connector.execute_readonly("SELECT count(*) AS n FROM customers") == ([{"n": 1}], 1)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA query_only")).scalar() == 1


def test_sqlite_engine_query_is_stopped_at_the_timeout(sqlite_keys_url: str) -> None:
    engine = create_engine(sqlite_keys_url, poolclass=StaticPool)
    connector = create_connector(engine, timeout_seconds=0)
    slow = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5000000) "
        "SELECT count(*) AS c FROM n"
    )

    with pytest.raises(Exception, match="interrupted"):
        connector.execute_readonly(slow)
    # The progress handler is removed afterwards, so the connection is usable again.
    with engine.connect() as conn:
        assert conn.execute(text(slow)).scalar() == 5000000


# --- Rendering and dangerous functions -----------------------------------------


def test_tsql_limit_is_rendered_as_top() -> None:
    schema = DatabaseSchema(
        dialect="tsql",
        tables=[TableSchema(name="customers", columns=[ColumnSchema(name="name", type="NVARCHAR")])],
    )
    validator = SQLValidator(QueryPilotConfig(dialect="tsql", max_rows=10))

    result = validator.validate("SELECT name FROM customers", schema)

    assert result.valid
    assert result.rewritten_sql == "SELECT TOP 10 name FROM customers"


class _StubConnector(BaseConnector):
    def __init__(self, dialect: str) -> None:
        super().__init__("stub://", 10)
        self.dialect = dialect

    def get_schema(self) -> DatabaseSchema:
        return DatabaseSchema(
            dialect=self.dialect,
            tables=[TableSchema(name="t", columns=[ColumnSchema(name="id", type="INTEGER")])],
        )

    def execute_readonly(self, sql: str) -> tuple[list[dict], int]:
        return [], 0


def _querypilot_for(dialect: str, tmp_path: Path) -> QueryPilot:
    if dialect == "sqlite":
        sqlite3.connect(tmp_path / "blocked.db").close()
        return QueryPilot.connect(f"sqlite:///{tmp_path / 'blocked.db'}")
    if dialect == "duckdb":
        duckdb = pytest.importorskip("duckdb")
        path = tmp_path / "blocked.duckdb"
        duckdb.connect(str(path)).close()
        return QueryPilot.connect(f"duckdb:///{path}")
    return QueryPilot(connector=_StubConnector(dialect), config=QueryPilotConfig(dialect=dialect))


_FROM_CLAUSE_FUNCTIONS = {"duckdb", "tsql"}


def _blocked_function_cases() -> list[tuple[str, str]]:
    return [
        (dialect, function)
        for dialect, functions in BLOCKED_FUNCTIONS_BY_DIALECT.items()
        for function in sorted(functions)
    ]


@pytest.mark.parametrize(("dialect", "function"), _blocked_function_cases())
def test_dangerous_function_is_rejected(dialect: str, function: str, tmp_path: Path) -> None:
    qp = _querypilot_for(dialect, tmp_path)
    in_from = dialect in _FROM_CLAUSE_FUNCTIONS and not function.startswith("xp_") and function != "getenv"
    sql = f"SELECT * FROM {function}('x')" if in_from else f"SELECT {function}('x')"

    result = qp.validate_sql(sql)

    assert not result.valid
    assert f"SQL function is blocked by policy: {function}" in result.errors


def test_default_policy_blocks_every_dialect_set() -> None:
    blocked = set(SafetyPolicy().blocked_functions)
    for functions in BLOCKED_FUNCTIONS_BY_DIALECT.values():
        assert functions <= blocked
