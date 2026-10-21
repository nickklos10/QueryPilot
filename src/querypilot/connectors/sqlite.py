from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from querypilot.connectors.base import BaseConnector
from querypilot.core.types import DatabaseSchema


class SQLiteConnector(BaseConnector):
    def __init__(self, database_url: str, timeout_seconds: int = 10) -> None:
        super().__init__(database_url, timeout_seconds)
        path = sqlite_database_path(database_url)
        if path is None:
            self.engine: Engine = create_engine(database_url)
            return
        if not path.exists():
            # Opening a missing path would silently create an empty database.
            raise FileNotFoundError(f"SQLite database not found: {path}")
        # Read-only at the connection level, matching the Postgres READ ONLY transaction,
        # so a validator gap can never turn into a write.
        uri = f"{path.resolve().as_uri()}?mode=ro"
        self.engine = create_engine(
            "sqlite://",
            creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
        )

    def get_schema(self) -> DatabaseSchema:
        from querypilot.schema.introspector import introspect_engine

        return introspect_engine(self.engine, "sqlite")

    def execute_readonly(self, sql: str) -> tuple[list[dict], int]:
        with self.engine.connect() as conn:
            conn.connection.set_progress_handler(
                _timeout_handler(self.timeout_seconds),
                1000,
            )
            result = conn.execute(text(sql))
            rows = [dict(row._mapping) for row in result.fetchall()]
        return rows, len(rows)


def sqlite_database_path(database_url: str) -> Path | None:
    """Return the file a sqlite URL points at, or None for in-memory databases."""
    database = make_url(database_url).database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database).expanduser()


def _timeout_handler(timeout_seconds: int):
    import time

    deadline = time.monotonic() + timeout_seconds

    def handler() -> int:
        return 1 if time.monotonic() > deadline else 0

    return handler
