"""Pick a connector and SQL dialect from a database URL or SQLAlchemy engine."""
from __future__ import annotations

from typing import Any

import sqlglot
from sqlalchemy.engine import URL, Engine, make_url

from querypilot.connectors.base import BaseConnector

# SQLAlchemy backend name -> sqlglot dialect name.
_BACKEND_DIALECTS = {
    backend: dialect
    for backend, dialect in {
        "awsathena": "athena",
        "bigquery": "bigquery",
        "clickhouse": "clickhouse",
        "clickhousedb": "clickhouse",
        "databricks": "databricks",
        "duckdb": "duckdb",
        "hive": "hive",
        "mariadb": "mysql",
        "mssql": "tsql",
        "mysql": "mysql",
        "oracle": "oracle",
        "postgres": "postgres",
        "postgresql": "postgres",
        "presto": "presto",
        "redshift": "redshift",
        "snowflake": "snowflake",
        "sqlite": "sqlite",
        "trino": "trino",
    }.items()
    if sqlglot.Dialect.get(dialect) is not None
}

_DIALECT_ALIASES = {
    "mariadb": "mysql",
    "mssql": "tsql",
    "postgresql": "postgres",
    "sqlserver": "tsql",
}


def normalize_dialect(dialect: str) -> str:
    lowered = dialect.lower()
    return _DIALECT_ALIASES.get(lowered, lowered)


def dialect_from_url(database_url: str | URL | Engine) -> str:
    """Infer the sqlglot dialect for a SQLAlchemy URL or engine."""
    backend = _url(database_url).get_backend_name()
    if backend not in _BACKEND_DIALECTS:
        raise ValueError(f"Cannot infer a SQL dialect from URL scheme '{backend}'. Pass dialect=...")
    return _BACKEND_DIALECTS[backend]


def create_connector(
    database_url: str | URL | Engine,
    dialect: str | None = None,
    *,
    timeout_seconds: int = 10,
    schemas: list[str] | None = None,
    engine_kwargs: dict[str, Any] | None = None,
) -> BaseConnector:
    """Return the connector for a URL or prebuilt engine.

    SQLite and Postgres URLs get dedicated connectors; everything else, and any
    prebuilt non-Postgres ``Engine``, goes through ``SQLAlchemyConnector``.
    """
    from querypilot.connectors.postgres import PostgresConnector
    from querypilot.connectors.sqlalchemy import SQLAlchemyConnector
    from querypilot.connectors.sqlite import SQLiteConnector

    resolved = normalize_dialect(dialect) if dialect else dialect_from_url(database_url)
    backend = _url(database_url).get_backend_name()
    is_engine = isinstance(database_url, Engine)

    if backend in {"postgres", "postgresql"}:
        return PostgresConnector(
            database_url,
            timeout_seconds=timeout_seconds,
            schemas=schemas,
            engine_kwargs=engine_kwargs,
        )
    if backend == "sqlite" and not is_engine:
        return SQLiteConnector(str(database_url), timeout_seconds=timeout_seconds)
    return SQLAlchemyConnector(
        database_url,
        timeout_seconds=timeout_seconds,
        schemas=schemas,
        engine_kwargs=engine_kwargs,
        dialect=resolved,
    )


def _url(database_url: str | URL | Engine) -> URL:
    if isinstance(database_url, Engine):
        return database_url.url
    return make_url(database_url)
