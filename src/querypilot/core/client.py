from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlalchemy.engine import Engine

from querypilot.access import AccessPolicy
from querypilot.adapters.toolkit import QueryPilotToolkit
from querypilot.audit import (
    AuditMetadata,
    AuditSink,
    InMemoryAuditSink,
    QueryAuditRecord,
)
from querypilot.connectors.base import BaseConnector
from querypilot.connectors.registry import (
    create_connector,
    dialect_from_url,
    normalize_dialect,
)
from querypilot.core.config import QueryPilotConfig, SafetyPolicy
from querypilot.core.types import (
    DatabaseSchema,
    GeneratedSQL,
    QueryPilotAnswer,
    QueryResult,
    SchemaMatch,
    TableSchema,
    ValidationResult,
)
from querypilot.execution.formatter import explain_result
from querypilot.execution.runner import execute
from querypilot.generation.sql_generator import DemoSQLGenerator, SQLGenerator
from querypilot.schema.enrich import enrich_schema
from querypilot.schema.pruning import prune_schema, visible_schema
from querypilot.schema.sampling import apply_samples, cached_samples, clear_sample_cache
from querypilot.schema.search import search_schema as search_schema_impl
from querypilot.validation.feedback import (
    clean_db_message,
    clean_error,
    describe_db_error,
    public_errors,
    repair_hints,
)
from querypilot.validation.validator import SQLValidator

# How much larger the schema context gets when the model cannot answer from a pruned one.
_EXPANDED_BUDGET_FACTOR = 4

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
        # Shared with with_audit_metadata() copies (sample values are cached per connector).
        self._schema_cache = _SchemaCache()

    @classmethod
    def connect(
        cls,
        database_url: str | Engine,
        dialect: str | None = None,
        readonly: bool = True,
        max_rows: int = 100,
        timeout_seconds: int = 10,
        max_generation_attempts: int = 3,
        allowed_tables: list[str] | None = None,
        blocked_tables: list[str] | None = None,
        safety_policy: SafetyPolicy | None = None,
        access_policy: AccessPolicy | None = None,
        generator: SQLGenerator | None = None,
        audit_sink: AuditSink | None = None,
        audit_metadata: AuditMetadata | None = None,
        *,
        schemas: list[str] | None = None,
        hints: list[str] | None = None,
        descriptions: dict[str, str] | None = None,
        sample_values: int = 3,
        schema_token_budget: int = 6000,
        engine_kwargs: dict[str, Any] | None = None,
    ) -> QueryPilot:
        """Connect to a SQLAlchemy URL or ``Engine``; the SQL dialect is inferred from it unless given.

        Drivers for databases other than SQLite and Postgres come from pip extras,
        e.g. ``pip install "querypilot[duckdb]"``.
        """
        resolved_dialect = normalize_dialect(dialect) if dialect else dialect_from_url(database_url)
        config = QueryPilotConfig(
            dialect=resolved_dialect,
            readonly=readonly,
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
            max_generation_attempts=max_generation_attempts,
            allowed_tables=allowed_tables,
            blocked_tables=blocked_tables,
            schemas=schemas,
            hints=hints or [],
            descriptions=descriptions or {},
            sample_values=sample_values,
            schema_token_budget=schema_token_budget,
            safety_policy=safety_policy or SafetyPolicy(),
            access_policy=access_policy or AccessPolicy(),
        )
        connector = create_connector(
            database_url,
            resolved_dialect,
            timeout_seconds=timeout_seconds,
            schemas=schemas,
            engine_kwargs=engine_kwargs,
        )
        return cls(
            connector=connector,
            config=config,
            generator=generator,
            audit_sink=audit_sink,
            audit_metadata=audit_metadata,
        )

    @classmethod
    def from_config(cls, path: str | Path | None = None, **overrides: Any) -> QueryPilot:
        """Connect using a querypilot.yaml project config (default: ./querypilot.yaml).

        Keyword overrides take precedence over the file, e.g. ``generator=`` or ``max_rows=``.
        """
        from querypilot.project import connect_project, find_config, load_project_config

        config_path = Path(path) if path is not None else find_config()
        if config_path is None:
            raise FileNotFoundError("No querypilot.yaml found; run `querypilot init` to create one.")
        return connect_project(load_project_config(config_path), **overrides)

    def get_schema(self) -> DatabaseSchema:
        """Return the introspected schema, cached after the first call.

        The cache is shared with every ``with_audit_metadata`` copy of this
        client, and concurrent first calls introspect only once.
        """
        cache = self._schema_cache
        schema = cache.schema
        if schema is None:
            with cache.lock:
                if cache.schema is None:
                    cache.schema = enrich_schema(
                        self.connector.get_schema(), self.connector, self.config
                    )
                schema = cache.schema
        return schema

    def refresh_schema(self) -> DatabaseSchema:
        """Drop the cached schema and sample values (for this client and its copies) and
        introspect the database again."""
        with self._schema_cache.lock:
            self._schema_cache.schema = None
        clear_sample_cache(self.connector)
        return self.get_schema()

    def visible_schema(self) -> DatabaseSchema:
        """The schema without the tables, columns and foreign keys the access policy hides.

        This is what agents may see; ``get_schema`` is the full, unfiltered schema.
        """
        return visible_schema(self.get_schema(), self.config)

    def table_details(self, table_names: list[str]) -> list[TableSchema]:
        """Return full table definitions (keys, comments, sample values) for the named tables.

        Tables and columns hidden by the access policy are left out, and sample
        values are only fetched for columns the policy lets the caller read
        unmasked and unfiltered. The results are copies.
        """
        schema = self.visible_schema()
        tables = [schema.get_table(name) for name in table_names]
        return [self._with_samples(table) for table in tables if table is not None]

    def schema_for_prompt(self, question: str, *, token_budget: int | None = None) -> DatabaseSchema:
        """Return the schema context sent to the generator for this question.

        The access-policy-visible schema, pruned to the tables relevant to the
        question only when it exceeds ``schema_token_budget`` (keeping
        foreign-key bridge tables), with sample values and ``config.hints``.
        """
        pruned = prune_schema(
            self.visible_schema(),
            question,
            self.config.schema_token_budget if token_budget is None else token_budget,
            sample_values=self.config.sample_values,
        )
        return pruned.model_copy(
            update={
                "tables": [self._with_samples(table) for table in pruned.tables],
                "hints": list(self.config.hints),
            }
        )

    def _with_samples(self, table: TableSchema) -> TableSchema:
        if self.config.sample_values <= 0:
            return table.model_copy(deep=True)
        samples = cached_samples(self.connector, table, self.config.dialect, self.config)
        return apply_samples(table, samples, limit=self.config.sample_values)

    def search_schema(self, query: str) -> list[SchemaMatch]:
        """Tables (and their matching columns) relevant to ``query``, within the visible schema."""
        matches = search_schema_impl(self.visible_schema(), query)
        self._write_audit_record(
            operation="schema_search",
            question=query,
            row_count=len(matches),
            executed=True,
        )
        return matches

    def generate_sql(self, question: str, schema: DatabaseSchema | None = None) -> GeneratedSQL:
        """Generate SQL for ``question`` from the prompt schema (see ``schema_for_prompt``)."""
        context = schema if schema is not None else self.schema_for_prompt(question)
        generated = self.generator.generate(question, context, self.config.max_rows)
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
            # The audit record keeps the full errors; the message leaves out hidden names.
            public = "; ".join(public_errors(validation.errors)) or details
            raise ValueError(f"SQL validation failed: {public}")
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
        """Generate, validate and run SQL for ``question``, repairing it when needed.

        Up to ``max_generation_attempts`` generations in total. A query that
        fails validation is sent back with the cleaned errors and "did you
        mean" hints; one that fails in the database is sent back with the
        cleaned database error; one that returns no rows gets one review
        asking to double-check filters and literals (if the model returns the
        same SQL, the empty result stands). Generators without ``repair`` get a
        single attempt. Raises ``ValueError`` with the last error on failure.
        """
        run = self._generate_and_run(question)
        if run.error is not None or run.result is None or run.validation is None:
            self._write_audit_record(
                operation="ask",
                question=question,
                sql=run.sql,
                rewritten_sql=run.validation.rewritten_sql if run.validation else None,
                validation=run.validation,
                valid=bool(run.validation and run.validation.valid),
                executed=False,
                access_policy=run.validation.access_policy if run.validation else {},
                error=run.audit_error or run.error,
            )
            raise ValueError(run.error or "SQL validation failed: unknown validation error")

        generated = run.generated
        validation = run.validation
        result = run.result
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
            generated_sql=generated.sql,
            rows=result.rows,
            explanation=explanation,
            validation=validation,
            execution_time_ms=result.execution_time_ms,
            access_policy=validation.access_policy,
        )

    def _generate_and_run(self, question: str) -> _GenerationRun:
        """The generate -> validate -> execute -> repair loop shared by ``ask`` and evals.

        Never raises for generation, validation or database errors; the
        outcome (and per-stage timings) is reported on the returned run.
        """
        run = _GenerationRun()
        max_attempts = self.config.max_generation_attempts
        can_repair = callable(getattr(self.generator, "repair", None))
        context = self.schema_for_prompt(question)
        hint_schema = self.visible_schema()

        started = time.perf_counter()
        generated = self.generate_sql(question, schema=context)
        run.generate_ms = _elapsed_ms(started)
        run.generated = generated
        attempts = 1
        reviewed_empty = False
        # A valid query that returned no rows, kept in case a later attempt fails.
        empty_fallback: tuple[GeneratedSQL, ValidationResult, QueryResult] | None = None

        while True:
            if generated.sql is None and attempts < max_attempts and self._schema_was_pruned(context):
                # The model could not answer from the pruned schema (often a table the
                # question names only indirectly): retry once with a much larger context.
                context = self.schema_for_prompt(
                    question, token_budget=self.config.schema_token_budget * _EXPANDED_BUDGET_FACTOR
                )
                started = time.perf_counter()
                generated = self.generate_sql(question, schema=context)
                run.generate_ms += _elapsed_ms(started)
                run.generated = generated
                run.repair_attempts += 1
                attempts += 1
                continue
            if generated.sql is None:
                details = "; ".join(clean_error(e) for e in generated.errors) or "no SQL was returned"
                if run.failure is None:
                    run.failure = "generation"
                    run.error = f"Could not generate SQL: {details}"
                break

            sql = generated.sql
            run.sql = sql
            started = time.perf_counter()
            validation = self.validate_sql(sql)
            run.validate_ms += _elapsed_ms(started)
            run.validation = validation

            outcome: str
            if not validation.valid or validation.rewritten_sql is None:
                # The model sees errors without the names of hidden tables and columns;
                # the audit record (audit_error) keeps the originals.
                errors = public_errors(validation.errors)
                details = "; ".join(errors) or "unknown validation error"
                run.failure, run.error = "validation", f"SQL validation failed: {details}"
                full = "; ".join(clean_error(error) for error in validation.errors)
                run.audit_error = f"SQL validation failed: {full or details}"
                hints = repair_hints(errors, hint_schema, sql)
                feedback = errors + [f"Hint: {hint}" for hint in hints]
                outcome = "validation"
            else:
                started = time.perf_counter()
                try:
                    result = self._execute_validated(validation)
                except Exception as exc:  # noqa: BLE001 - database errors become feedback
                    run.execute_ms += _elapsed_ms(started)
                    # The model's own SQL, not the rewritten one: redaction keeps its
                    # names and literals but not row-filter values.
                    lines = describe_db_error(exc, hint_schema, sql)
                    run.failure = "execution"
                    run.error = f"SQL execution failed: {lines[0].removeprefix('Database error: ')}"
                    run.audit_error = f"SQL execution failed: {clean_db_message(exc)}"
                    feedback = [lines[0], *(f"Hint: {hint}" for hint in lines[1:])]
                    outcome = "execution"
                else:
                    run.execute_ms += _elapsed_ms(started)
                    if result.row_count > 0 or reviewed_empty or not can_repair or attempts >= max_attempts:
                        run.succeed(generated, validation, result)
                        return run
                    empty_fallback = (generated, validation, result)
                    run.failure, run.error, run.audit_error = None, None, None
                    reviewed_empty = True
                    feedback = [EMPTY_RESULT_FEEDBACK]
                    outcome = "empty"

            run.feedback.append(feedback)
            if not can_repair or attempts >= max_attempts:
                break

            started = time.perf_counter()
            generated = self.generator.repair(  # type: ignore[attr-defined]
                question,
                context,
                self.config.max_rows,
                sql,
                _repair_validation(validation, feedback),
            )
            run.repair_ms += _elapsed_ms(started)
            run.repair_attempts += 1
            attempts += 1
            run.generated = generated
            if outcome == "empty" and generated.sql is not None and _same_sql(
                generated.sql, (sql, validation.rewritten_sql), self.config.dialect
            ):
                break

        if empty_fallback is not None:
            run.succeed(*empty_fallback)
        return run

    def _schema_was_pruned(self, context: DatabaseSchema) -> bool:
        return len(context.tables) < len(self.visible_schema().tables)

    def _execute_validated(self, validation: ValidationResult) -> QueryResult:
        assert validation.rewritten_sql is not None
        result = execute(self.connector, validation.rewritten_sql)
        result.rows = self._apply_masking(result.rows, validation)
        result.access_policy = validation.access_policy
        return result

    def toolkit(self, **options: Any) -> QueryPilotToolkit:
        """All agent tools (list_tables, describe_table, run_sql, ...) bound to this client.

        Options are passed to ``QueryPilotToolkit``, e.g. ``include_ask=True``.
        """
        return QueryPilotToolkit(self, **options)

    def as_openai_tools(self) -> list[dict[str, Any]]:
        return self.toolkit().openai_tools()

    def as_anthropic_tools(self) -> list[dict[str, Any]]:
        return self.toolkit().anthropic_tools()

    def handle_tool_call(
        self, name: str, arguments: dict[str, Any] | str | None = None
    ) -> dict[str, Any]:
        """Run an OpenAI or Anthropic tool call; errors come back as ``{"ok": False, ...}``."""
        return self.toolkit().dispatch(name, arguments)

    def handle_anthropic_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        return self.handle_tool_call(tool_name, tool_input)

    def get_audit_records(self, limit: int = 100) -> list[QueryAuditRecord]:
        return self.audit_sink.recent(limit)

    def with_audit_metadata(self, metadata: AuditMetadata | None) -> QueryPilot:
        scoped = QueryPilot(
            connector=self.connector,
            config=self.config,
            generator=self.generator,
            audit_sink=self.audit_sink,
            audit_metadata=metadata or AuditMetadata(),
        )
        scoped._schema_cache = self._schema_cache
        return scoped

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


def _mask_value(value: Any, mode: str) -> Any:
    if mode == "null":
        return None
    if mode == "hash":
        import hashlib

        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return "[REDACTED]"


EMPTY_RESULT_FEEDBACK = (
    "The query ran without errors but returned no rows. Double-check the filters and "
    "literal values against the stored values shown as e.g. samples in the schema "
    "(spelling, case, date and number formats) and the join conditions. If no rows is "
    "the correct answer, return the same query unchanged."
)


class _SchemaCache:
    """The introspected schema, shared by a client and its ``with_audit_metadata`` copies."""

    def __init__(self) -> None:
        self.schema: DatabaseSchema | None = None
        self.lock = threading.Lock()


@dataclass
class _GenerationRun:
    """Outcome of ``QueryPilot._generate_and_run``."""

    generated: GeneratedSQL | None = None
    sql: str | None = None
    validation: ValidationResult | None = None
    result: QueryResult | None = None
    # "generation", "validation" or "execution" when the run failed.
    failure: str | None = None
    # ``error`` is safe to show the caller (no hidden names, no stored values);
    # ``audit_error`` is the unredacted version for audit records and evals.
    error: str | None = None
    audit_error: str | None = None
    repair_attempts: int = 0
    # Feedback sent with each repair request, in order.
    feedback: list[list[str]] = field(default_factory=list)
    generate_ms: int = 0
    validate_ms: int = 0
    repair_ms: int = 0
    execute_ms: int = 0

    def succeed(
        self, generated: GeneratedSQL, validation: ValidationResult, result: QueryResult
    ) -> None:
        self.generated = generated
        self.sql = generated.sql
        self.validation = validation
        self.result = result
        self.failure = None
        self.error = None
        self.audit_error = None


def _repair_validation(validation: ValidationResult, feedback: list[str]) -> ValidationResult:
    """The validation handed to ``repair``: the feedback as errors, without the fields
    that can name hidden objects or show row-filter predicates."""
    return validation.model_copy(
        update={
            "valid": False,
            "errors": feedback,
            "rewritten_sql": None,
            "blocked_reason": None,
            "policy_checks": [],
            "access_policy": {},
        }
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _same_sql(candidate: str, previous: tuple[str | None, ...], dialect: str) -> bool:
    """True when ``candidate`` is one of ``previous`` up to formatting."""
    normalized = _normalize_sql(candidate, dialect)
    return any(sql is not None and _normalize_sql(sql, dialect) == normalized for sql in previous)


def _normalize_sql(sql: str, dialect: str) -> str:
    text = sql.strip().rstrip(";").strip()
    try:
        return sqlglot.parse_one(text, read=dialect).sql(dialect=dialect, normalize=True)
    except Exception:  # noqa: BLE001 - fall back to whitespace/case folding
        return " ".join(text.lower().split())
