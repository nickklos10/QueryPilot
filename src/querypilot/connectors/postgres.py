from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, Engine

from querypilot.connectors.base import BaseConnector
from querypilot.connectors.sqlalchemy import build_engine
from querypilot.core.types import DatabaseSchema


class PostgresConnector(BaseConnector):
    def __init__(
        self,
        database_url: str | URL | Engine,
        timeout_seconds: int = 10,
        *,
        schemas: list[str] | None = None,
        engine_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(database_url, Engine):
            self.engine: Engine = database_url
            display_url = database_url.url.render_as_string(hide_password=True)
        else:
            url = database_url if isinstance(database_url, str) else database_url.render_as_string(False)
            self.engine = build_engine(_normalize_postgres_url(url), engine_kwargs)
            display_url = str(database_url)
        super().__init__(display_url, timeout_seconds)
        self.schemas = schemas

    def get_schema(self) -> DatabaseSchema:
        from querypilot.schema.introspector import introspect_engine

        return introspect_engine(self.engine, "postgres", self.schemas)

    def execute_readonly(self, sql: str) -> tuple[list[dict], int]:
        with self.engine.connect() as conn:
            readonly_result = conn.execute(text("SET TRANSACTION READ ONLY"))
            readonly_result.close()
            timeout_result = conn.execute(
                text(f"SET LOCAL statement_timeout = {self.timeout_seconds * 1000}")
            )
            timeout_result.close()
            query_result = conn.execute(text(sql))
            rows = [dict(row._mapping) for row in query_result.fetchall()]
        return rows, len(rows)


def _normalize_postgres_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return database_url
