from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from querypilot.connectors.base import BaseConnector
from querypilot.core.types import ColumnSchema, DatabaseSchema, TableSchema


class SQLiteConnector(BaseConnector):
    def __init__(self, database_url: str, timeout_seconds: int = 10) -> None:
        super().__init__(database_url, timeout_seconds)
        self.engine: Engine = create_engine(database_url)

    def get_schema(self) -> DatabaseSchema:
        inspector = inspect(self.engine)
        tables: list[TableSchema] = []
        for table_name in inspector.get_table_names():
            columns = [
                ColumnSchema(
                    name=column["name"],
                    type=str(column["type"]),
                    nullable=bool(column.get("nullable", True)),
                )
                for column in inspector.get_columns(table_name)
            ]
            tables.append(TableSchema(name=table_name, columns=columns))
        return DatabaseSchema(dialect="sqlite", tables=tables)

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
