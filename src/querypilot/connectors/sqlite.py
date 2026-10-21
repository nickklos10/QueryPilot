from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from querypilot.connectors.base import BaseConnector
from querypilot.core.types import DatabaseSchema


class SQLiteConnector(BaseConnector):
    def __init__(self, database_url: str, timeout_seconds: int = 10) -> None:
        super().__init__(database_url, timeout_seconds)
        self.engine: Engine = create_engine(database_url)

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


def _timeout_handler(timeout_seconds: int):
    import time

    deadline = time.monotonic() + timeout_seconds

    def handler() -> int:
        return 1 if time.monotonic() > deadline else 0

    return handler
