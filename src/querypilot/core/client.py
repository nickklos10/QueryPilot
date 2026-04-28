from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from querypilot.adapters.anthropic import anthropic_tools
from querypilot.adapters.openai import openai_tools
from querypilot.connectors.base import BaseConnector
from querypilot.connectors.postgres import PostgresConnector
from querypilot.connectors.sqlite import SQLiteConnector
from querypilot.core.config import QueryPilotConfig, SafetyPolicy
from querypilot.core.types import (
    DatabaseSchema,
    GeneratedSQL,
    QueryPilotAnswer,
    QueryResult,
    SchemaMatch,
    ValidationResult,
)
from querypilot.execution.formatter import explain_result
from querypilot.execution.runner import execute
from querypilot.generation.sql_generator import DemoSQLGenerator, SQLGenerator
from querypilot.schema.search import search_schema as search_schema_impl
from querypilot.validation.validator import SQLValidator


class QueryPilot:
    def __init__(
        self,
        connector: BaseConnector,
        config: QueryPilotConfig,
        generator: SQLGenerator | None = None,
    ) -> None:
        self.connector = connector
        self.config = config
        self.generator = generator or DemoSQLGenerator()
        self.validator = SQLValidator(config)

    @classmethod
    def connect(
        cls,
        database_url: str,
        dialect: str = "sqlite",
        readonly: bool = True,
        max_rows: int = 100,
        timeout_seconds: int = 10,
        allowed_tables: list[str] | None = None,
        blocked_tables: list[str] | None = None,
        safety_policy: SafetyPolicy | None = None,
        generator: SQLGenerator | None = None,
    ) -> "QueryPilot":
        config = QueryPilotConfig(
            dialect=dialect,
            readonly=readonly,
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
            allowed_tables=allowed_tables,
            blocked_tables=blocked_tables,
            safety_policy=safety_policy or SafetyPolicy(),
        )
        connector = _connector_for(database_url, dialect, timeout_seconds)
        return cls(connector=connector, config=config, generator=generator)

    def get_schema(self) -> DatabaseSchema:
        return self.connector.get_schema()

    def search_schema(self, query: str) -> list[SchemaMatch]:
        return search_schema_impl(self.get_schema(), query)

    def generate_sql(self, question: str) -> GeneratedSQL:
        return self.generator.generate(question, self.get_schema(), self.config.max_rows)

    def validate_sql(self, sql: str) -> ValidationResult:
        return self.validator.validate(sql, self.get_schema())

    def execute_sql(self, sql: str) -> QueryResult:
        validation = self.validate_sql(sql)
        if not validation.valid or validation.rewritten_sql is None:
            details = "; ".join(validation.errors) or "unknown validation error"
            raise ValueError(f"SQL validation failed: {details}")
        return execute(self.connector, validation.rewritten_sql)

    def ask(self, question: str) -> QueryPilotAnswer:
        generated = self.generate_sql(question)
        if generated.sql is None:
            details = "; ".join(generated.errors) or "no SQL was returned"
            raise ValueError(f"Could not generate SQL: {details}")

        validation = self.validate_sql(generated.sql)
        if not validation.valid or validation.rewritten_sql is None:
            details = "; ".join(validation.errors) or "unknown validation error"
            raise ValueError(f"SQL validation failed: {details}")

        result = execute(self.connector, validation.rewritten_sql)
        explanation = generated.explanation or explain_result(question, result.sql, result.rows)
        return QueryPilotAnswer(
            question=question,
            sql=result.sql,
            rows=result.rows,
            explanation=explanation,
            validation=validation,
            execution_time_ms=result.execution_time_ms,
        )

    def as_openai_tools(self) -> list[dict[str, Any]]:
        return openai_tools()

    def as_anthropic_tools(self) -> list[dict[str, Any]]:
        return anthropic_tools()

    def handle_anthropic_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        if tool_name == "ask_database":
            return self.ask(str(tool_input["question"])).model_dump()
        if tool_name == "search_schema":
            matches = self.search_schema(str(tool_input["query"]))
            return TypeAdapter(list[SchemaMatch]).dump_python(matches)
        if tool_name == "validate_sql":
            return self.validate_sql(str(tool_input["sql"])).model_dump()
        if tool_name == "execute_sql":
            return self.execute_sql(str(tool_input["sql"])).model_dump()
        raise ValueError(f"Unknown Anthropic tool: {tool_name}")


def _connector_for(database_url: str, dialect: str, timeout_seconds: int) -> BaseConnector:
    normalized = dialect.lower()
    if normalized == "sqlite":
        return SQLiteConnector(database_url, timeout_seconds=timeout_seconds)
    if normalized in {"postgres", "postgresql"}:
        return PostgresConnector(database_url, timeout_seconds=timeout_seconds)
    raise ValueError(f"Unsupported dialect: {dialect}")
