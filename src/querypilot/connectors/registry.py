"""Pick a connector and SQL dialect from a database URL."""
from __future__ import annotations

from typing import Any

from sqlalchemy.engine import make_url

from querypilot.connectors.base import BaseConnector

# SQLAlchemy backend name -> sqlglot dialect name.
_BACKEND_DIALECTS = {
    "sqlite": "sqlite",
    "postgresql": "postgres",
    "postgres": "postgres",
}

_DIALECT_ALIASES = {
    "postgresql": "postgres",
}


def normalize_dialect(dialect: str) -> str:
    lowered = dialect.lower()
    return _DIALECT_ALIASES.get(lowered, lowered)


def dialect_from_url(database_url: str) -> str:
    """Infer the sqlglot dialect for a SQLAlchemy-style URL."""
    backend = make_url(database_url).get_backend_name()
    if backend not in _BACKEND_DIALECTS:
        raise ValueError(f"Cannot infer a SQL dialect from URL scheme '{backend}'. Pass dialect=...")
    return _BACKEND_DIALECTS[backend]


def create_connector(
    database_url: str,
    dialect: str | None = None,
    *,
    timeout_seconds: int = 10,
    schemas: list[str] | None = None,
    engine_kwargs: dict[str, Any] | None = None,
) -> BaseConnector:
    from querypilot.connectors.postgres import PostgresConnector
    from querypilot.connectors.sqlite import SQLiteConnector

    resolved = normalize_dialect(dialect) if dialect else dialect_from_url(database_url)
    if resolved == "sqlite":
        return SQLiteConnector(database_url, timeout_seconds=timeout_seconds)
    if resolved == "postgres":
        return PostgresConnector(database_url, timeout_seconds=timeout_seconds)
    raise ValueError(f"Unsupported dialect: {dialect}")
