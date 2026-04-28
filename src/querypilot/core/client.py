from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from querypilot.access import AccessPolicy
from querypilot.adapters.anthropic import anthropic_tools
from querypilot.adapters.openai import openai_tools
from querypilot.audit import AuditMetadata, AuditSink, InMemoryAuditSink, QueryAuditRecord
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
        audit_sink: AuditSink | None = None,
        audit_metadata: AuditMetadata | None = None,
    ) -> None:
        self.connector = connector
        self.config = config
        self.generator = generator or DemoSQLGenerator()
        self.validator = SQLValidator(config)
        self.audit_sink = audit_sink or InMemoryAuditSink()
        self.audit_metadata = audit_metadata or AuditMetadata()

    @classmethod
    def connect(
        cls,
        database_url: str,
        dialect: str = "sqlite",
        readonly: bool = True,
        max_rows: int = 100,
        timeout_seconds: int = 10,
        max_generation_attempts: int = 2,
        allowed_tables: list[str] | None = None,
        blocked_tables: list[str] | None = None,
        safety_policy: SafetyPolicy | None = None,
        access_policy: AccessPolicy | None = None,
        generator: SQLGenerator | None = None,
        audit_sink: AuditSink | None = None,
        audit_metadata: AuditMetadata | None = None,
    ) -> "QueryPilot":
        config = QueryPilotConfig(
            dialect=dialect,
            readonly=readonly,
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
            max_generation_attempts=max_generation_attempts,
            allowed_tables=allowed_tables,
            blocked_tables=blocked_tables,
            safety_policy=safety_policy or SafetyPolicy(),
            access_policy=access_policy or AccessPolicy(),
        )
        connector = _connector_for(database_url, dialect, timeout_seconds)
        return cls(
            connector=connector,
            config=config,
            generator=generator,
            audit_sink=audit_sink,
            audit_metadata=audit_metadata,
        )

    def get_schema(self) -> DatabaseSchema:
        return self.connector.get_schema()

    def search_schema(self, query: str) -> list[SchemaMatch]:
        matches = search_schema_impl(self.get_schema(), query)
        self._write_audit_record(
            operation="schema_search",
            question=query,
            row_count=len(matches),
            executed=True,
        )
        return matches

    def generate_sql(self, question: str) -> GeneratedSQL:
        generated = self.generator.generate(question, self.get_schema(), self.config.max_rows)
        self._write_audit_record(
            operation="generate_sql",
            question=question,
            sql=generated.sql,
            error="; ".join(generated.errors) if generated.errors else None,
            executed=generated.sql is not None,
        )
        return generated

    def validate_sql(self, sql: str) -> ValidationResult:
        validation = self.validator.validate(sql, self.get_schema())
        record = self._write_audit_record(
            operation="validate_sql",
            sql=sql,
            rewritten_sql=validation.rewritten_sql,
                validation=validation,
                valid=validation.valid,
                access_policy=validation.access_policy,
                error="; ".join(validation.errors) if validation.errors else None,
            )
        validation.audit_id = record.audit_id
        return validation

    def execute_sql(self, sql: str) -> QueryResult:
        validation = self.validator.validate(sql, self.get_schema())
        if not validation.valid or validation.rewritten_sql is None:
            details = "; ".join(validation.errors) or "unknown validation error"
            self._write_audit_record(
                operation="execute_sql",
                sql=sql,
                rewritten_sql=validation.rewritten_sql,
                validation=validation,
                valid=validation.valid,
                executed=False,
                access_policy=validation.access_policy,
                error=details,
            )
            raise ValueError(f"SQL validation failed: {details}")
        result = execute(self.connector, validation.rewritten_sql)
        result.rows = self._apply_masking(result.rows, validation)
        result.access_policy = validation.access_policy
        record = self._write_audit_record(
            operation="execute_sql",
            sql=sql,
            rewritten_sql=result.sql,
            validation=validation,
            valid=True,
            executed=True,
            row_count=result.row_count,
            execution_time_ms=result.execution_time_ms,
            access_policy=validation.access_policy,
        )
        validation.audit_id = record.audit_id
        result.audit_id = record.audit_id
        return result

    def ask(self, question: str) -> QueryPilotAnswer:
        generated = self.generate_sql(question)
        validation: ValidationResult | None = None

        for attempt in range(self.config.max_generation_attempts):
            if generated.sql is None:
                details = "; ".join(generated.errors) or "no SQL was returned"
                raise ValueError(f"Could not generate SQL: {details}")

            validation = self.validate_sql(generated.sql)
            if validation.valid and validation.rewritten_sql is not None:
                break

            can_repair = hasattr(self.generator, "repair")
            if not can_repair or attempt >= self.config.max_generation_attempts - 1:
                details = "; ".join(validation.errors) or "unknown validation error"
                raise ValueError(f"SQL validation failed: {details}")

            generated = self.generator.repair(  # type: ignore[attr-defined]
                question,
                self.get_schema(),
                self.config.max_rows,
                generated.sql,
                validation,
            )

        if validation is None or not validation.valid or validation.rewritten_sql is None:
            raise ValueError("SQL validation failed: unknown validation error")

        result = execute(self.connector, validation.rewritten_sql)
        result.rows = self._apply_masking(result.rows, validation)
        result.access_policy = validation.access_policy
        explanation = generated.explanation or explain_result(question, result.sql, result.rows)
        record = self._write_audit_record(
            operation="ask",
            question=question,
            sql=generated.sql,
            rewritten_sql=result.sql,
            validation=validation,
            valid=True,
            executed=True,
            row_count=result.row_count,
            execution_time_ms=result.execution_time_ms,
            access_policy=validation.access_policy,
        )
        return QueryPilotAnswer(
            audit_id=record.audit_id,
            question=question,
            sql=result.sql,
            rows=result.rows,
            explanation=explanation,
            validation=validation,
            execution_time_ms=result.execution_time_ms,
            access_policy=validation.access_policy,
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

    def get_audit_records(self, limit: int = 100) -> list[QueryAuditRecord]:
        return self.audit_sink.recent(limit)

    def with_audit_metadata(self, metadata: AuditMetadata | None) -> "QueryPilot":
        return QueryPilot(
            connector=self.connector,
            config=self.config,
            generator=self.generator,
            audit_sink=self.audit_sink,
            audit_metadata=metadata or AuditMetadata(),
        )

    def _write_audit_record(self, operation: str, **kwargs) -> QueryAuditRecord:
        record = QueryAuditRecord.create(
            operation=operation,
            metadata=self.audit_metadata,
            **kwargs,
        )
        self.audit_sink.write(record)
        return record

    def _apply_masking(self, rows: list[dict], validation: ValidationResult) -> list[dict]:
        if not rows or not self.config.access_policy.masking_rules:
            return rows

        rules: dict[str, str] = {}
        for table_name in validation.tables:
            for column_name, rule in self.config.access_policy.masking_rules.get(table_name, {}).items():
                rules[column_name] = rule.mode

        if not rules:
            return rows

        return [
            {
                key: _mask_value(value, rules[key]) if key in rules else value
                for key, value in row.items()
            }
            for row in rows
        ]


def _connector_for(database_url: str, dialect: str, timeout_seconds: int) -> BaseConnector:
    normalized = dialect.lower()
    if normalized == "sqlite":
        return SQLiteConnector(database_url, timeout_seconds=timeout_seconds)
    if normalized in {"postgres", "postgresql"}:
        return PostgresConnector(database_url, timeout_seconds=timeout_seconds)
    raise ValueError(f"Unsupported dialect: {dialect}")


def _mask_value(value: Any, mode: str) -> Any:
    if mode == "null":
        return None
    if mode == "hash":
        import hashlib

        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return "[REDACTED]"
