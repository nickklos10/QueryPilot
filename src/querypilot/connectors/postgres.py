from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from querypilot.connectors.base import BaseConnector
from querypilot.core.types import ColumnSchema, DatabaseSchema, TableSchema


class PostgresConnector(BaseConnector):
    def __init__(self, database_url: str, timeout_seconds: int = 10) -> None:
        super().__init__(database_url, timeout_seconds)
        self.engine: Engine = create_engine(_normalize_postgres_url(database_url))

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
        return DatabaseSchema(dialect="postgres", tables=tables)

    def execute_readonly(self, sql: str) -> tuple[list[dict], int]:
        with self.engine.connect() as conn:
            result = conn.execute(
                text(f"SET LOCAL statement_timeout = {self.timeout_seconds * 1000}")
            )
            result.close()
            query_result = conn.execute(text(sql))
            rows = [dict(row._mapping) for row in query_result.fetchall()]
        return rows, len(rows)


def _normalize_postgres_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return database_url
