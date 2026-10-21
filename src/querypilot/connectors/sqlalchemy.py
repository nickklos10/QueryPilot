"""Connector for any database with a SQLAlchemy dialect."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import NoSuchModuleError

from querypilot.connectors.base import BaseConnector
from querypilot.core.types import DatabaseSchema

# URL backend -> the querypilot extra that installs its driver.
DRIVER_EXTRAS = {
    "bigquery": "bigquery",
    "databricks": "databricks",
    "duckdb": "duckdb",
    "mariadb": "mysql",
    "mssql": "mssql",
    "mysql": "mysql",
    "oracle": "oracle",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "trino": "trino",
}

# Driver used when the URL names none, matching the driver each extra installs.
_DEFAULT_DRIVERS = {"mariadb": "pymysql", "mysql": "pymysql", "oracle": "oracledb"}


class SQLAlchemyConnector(BaseConnector):
    """Read-only connector for any SQLAlchemy-supported database.

    Every query runs in a transaction that is always rolled back. Where the engine has
    them, native guards are added on top:

    - DuckDB: file databases open with ``read_only=True``; queries are interrupted at the timeout.
    - MySQL / MariaDB: ``START TRANSACTION READ ONLY`` plus a per-session statement timeout.
    - Oracle: ``SET TRANSACTION READ ONLY``.
    - Snowflake: ``STATEMENT_TIMEOUT_IN_SECONDS``; Redshift: ``statement_timeout``.
    - SQLite (when given an Engine): ``PRAGMA query_only`` for the duration of the query
      (then restored to its previous value) and a progress-handler deadline at the timeout.

    Other engines (SQL Server, BigQuery, Trino, Databricks, ...) rely on rollback alone,
    which does not undo non-transactional side effects. Connect with a database role
    that can only read; that is the real guarantee, and the validator is a second line.
    """

    def __init__(
        self,
        database_url: str | URL | Engine,
        timeout_seconds: int = 10,
        *,
        schemas: list[str] | None = None,
        engine_kwargs: dict[str, Any] | None = None,
        dialect: str | None = None,
    ) -> None:
        if isinstance(database_url, Engine):
            self.engine = database_url
        else:
            self.engine = build_engine(database_url, engine_kwargs)
        super().__init__(_display_url(database_url, self.engine), timeout_seconds)
        self.schemas = schemas
        self.backend = self.engine.dialect.name
        self.dialect = dialect or _sqlglot_dialect(self.engine)

    def get_schema(self) -> DatabaseSchema:
        from querypilot.schema.introspector import introspect_engine

        return introspect_engine(self.engine, self.dialect, self.schemas)

    def execute_readonly(self, sql: str) -> tuple[list[dict], int]:
        with self.engine.connect() as conn:
            transaction = conn.begin()
            resets: list[str] = []
            try:
                resets = self._reset_statements(conn)
                for statement in self._session_statements(conn):
                    conn.execute(text(statement))
                with self._timeout(conn):
                    result = conn.execute(text(sql))
                    rows = [dict(row._mapping) for row in result.fetchall()]
            finally:
                try:
                    transaction.rollback()
                finally:
                    # Session settings are restored even when the rollback fails.
                    for statement in resets:
                        conn.execute(text(statement))
                        conn.rollback()
        return rows, len(rows)

    def _session_statements(self, conn: Connection | None = None) -> list[str]:
        seconds = self.timeout_seconds
        if self.backend in {"mysql", "mariadb"} and _is_mariadb(conn, self.backend):
            # MariaDB rejects MAX_EXECUTION_TIME, also when reached via a mysql:// URL.
            return [f"SET SESSION max_statement_time = {seconds}", "START TRANSACTION READ ONLY"]
        if self.backend == "mysql":
            return [
                f"SET SESSION MAX_EXECUTION_TIME = {seconds * 1000}",
                "START TRANSACTION READ ONLY",
            ]
        if self.backend == "oracle":
            return ["SET TRANSACTION READ ONLY"]
        if self.backend == "snowflake":
            return [f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {seconds}"]
        if self.backend == "redshift":
            return [f"SET statement_timeout TO {seconds * 1000}"]
        if self.backend == "sqlite":
            return ["PRAGMA query_only = ON"]
        return []

    def _reset_statements(self, conn: Connection) -> list[str]:
        """Statements that restore the session settings changed for the query."""
        if self.backend == "sqlite":
            previous = conn.execute(text("PRAGMA query_only")).scalar()
            return [f"PRAGMA query_only = {1 if previous else 0}"]
        return []

    @contextmanager
    def _timeout(self, conn: Connection):
        """Stop the query at the timeout on drivers that support it (DuckDB, SQLite)."""
        dbapi_connection = conn.connection.dbapi_connection
        if self.backend == "sqlite":
            set_handler = getattr(dbapi_connection, "set_progress_handler", None)
            if set_handler is not None:
                set_handler(_deadline_handler(self.timeout_seconds), 1000)
                try:
                    yield
                finally:
                    set_handler(None, 1000)
                return
        interrupt = None
        if self.backend == "duckdb":
            interrupt = getattr(dbapi_connection, "interrupt", None)
        if interrupt is None:
            yield
            return
        timer = threading.Timer(self.timeout_seconds, interrupt)
        timer.start()
        try:
            yield
        finally:
            timer.cancel()


def _is_mariadb(conn: Connection | None, backend: str) -> bool:
    """MariaDB is detected on the connection: a mysql+pymysql:// URL can reach either server."""
    if backend == "mariadb":
        return True
    dialect = getattr(conn, "dialect", None)
    return bool(getattr(dialect, "is_mariadb", False))


def _deadline_handler(timeout_seconds: int):
    """SQLite progress handler that aborts the statement once the timeout has passed."""
    deadline = time.monotonic() + timeout_seconds

    def handler() -> int:
        return 1 if time.monotonic() > deadline else 0

    return handler


def build_engine(database_url: str | URL, engine_kwargs: dict[str, Any] | None = None) -> Engine:
    """Create an engine, defaulting the driver and DuckDB read-only mode, with a clear
    ImportError naming the pip extra when the driver is missing."""
    url = with_default_driver(make_url(database_url))
    kwargs: dict[str, Any] = {"pool_pre_ping": True, **(engine_kwargs or {})}
    if url.get_backend_name() == "duckdb" and _is_duckdb_file(url):
        kwargs["connect_args"] = {"read_only": True, **kwargs.get("connect_args", {})}
    try:
        return create_engine(url, **kwargs)
    except (NoSuchModuleError, ImportError) as exc:
        raise missing_driver_error(url.get_backend_name()) from exc


def with_default_driver(url: URL) -> URL:
    backend = url.get_backend_name()
    if "+" not in url.drivername and backend in _DEFAULT_DRIVERS:
        return url.set(drivername=f"{backend}+{_DEFAULT_DRIVERS[backend]}")
    return url


def missing_driver_error(backend: str) -> ImportError:
    extra = DRIVER_EXTRAS.get(backend)
    if extra is None:
        return ImportError(
            f"No SQLAlchemy driver is installed for '{backend}' URLs. "
            "Install the dialect package for your database."
        )
    return ImportError(
        f"The driver for '{backend}' URLs is not installed. "
        f'Install it with: pip install "querypilot[{extra}]"'
    )


def _is_duckdb_file(url: URL) -> bool:
    database = url.database or ""
    return bool(database) and database != ":memory:" and not database.startswith(("md:", "motherduck:"))


def _display_url(database_url: str | URL | Engine, engine: Engine) -> str:
    if isinstance(database_url, str):
        return database_url
    return engine.url.render_as_string(hide_password=True)


def _sqlglot_dialect(engine: Engine) -> str:
    from querypilot.connectors.registry import dialect_from_url

    try:
        return dialect_from_url(engine.url)
    except ValueError:
        return engine.dialect.name
