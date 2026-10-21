"""One definition of every QueryPilot agent tool, rendered for any provider or framework."""

from __future__ import annotations

import base64
import datetime as dt
import difflib
import json
import math
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from querypilot.adapters.anthropic import anthropic_tool
from querypilot.adapters.openai import openai_responses_tool, openai_tool
from querypilot.core.types import ColumnSchema, DatabaseSchema, TableSchema
from querypilot.validation.feedback import (
    clean_error,
    describe_db_error,
    public_errors,
    redact_values,
    repair_hints,
)

if TYPE_CHECKING:
    from querypilot.core.client import QueryPilot

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]

TOOL_KEYS = (
    "list_tables",
    "describe_table",
    "search_schema",
    "validate_sql",
    "run_sql",
    "ask_database",
)
ROW_FILTER_NOTE = (
    "Row-level filters from the access policy were applied, so the results only "
    "include rows you are allowed to see."
)
# Older names that still dispatch, but are not advertised to the model.
TOOL_ALIASES = {"execute_sql": "run_sql"}

READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

_TITLES = {
    "list_tables": "List tables",
    "describe_table": "Describe tables",
    "search_schema": "Search schema",
    "validate_sql": "Validate SQL",
    "run_sql": "Run SQL",
    "ask_database": "Ask the database",
}


class QueryPilotTool(BaseModel):
    """A provider-neutral tool: what the model sees plus the handler that answers it."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    key: str
    name: str
    title: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler = Field(exclude=True, repr=False)

    def __call__(self, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.handler(dict(arguments or {}))


class QueryPilotToolkit:
    """Every QueryPilot tool, defined once, with structured results and no raised errors.

    ``dispatch`` never raises: failures come back as ``{"ok": False, "error", "hints"}``
    so the model can fix its call and retry.
    """

    def __init__(
        self,
        querypilot: QueryPilot,
        *,
        include_ask: bool | None = None,
        names: Mapping[str, str] | None = None,
        max_payload_chars: int = 20_000,
        max_cell_chars: int = 500,
    ) -> None:
        self.querypilot = querypilot
        self.max_payload_chars = max_payload_chars
        self.max_cell_chars = max_cell_chars
        self._names = {key: key for key in TOOL_KEYS}
        self._names.update(names or {})
        if include_ask is None:
            from querypilot.generation.sql_generator import DemoSQLGenerator

            include_ask = not isinstance(querypilot.generator, DemoSQLGenerator)
        self.include_ask = include_ask
        self._tools = [
            self._build(key) for key in TOOL_KEYS if key != "ask_database" or include_ask
        ]

    # ------------------------------------------------------------------ definitions

    @property
    def tools(self) -> list[QueryPilotTool]:
        return list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self._tools]

    def name_of(self, key: str) -> str:
        return self._names.get(key, key)

    def get(self, name: str) -> QueryPilotTool | None:
        key = TOOL_ALIASES.get(name, name)
        for tool in self._tools:
            if name == tool.name or key == tool.key:
                return tool
        if key == "ask_database" and not self.include_ask:
            # Callable when asked for by name even if it is not advertised.
            return self._build("ask_database")
        return None

    def openai_tools(self) -> list[dict[str, Any]]:
        """Tool definitions for the OpenAI Chat Completions API."""
        return [openai_tool(tool.name, tool.description, tool.parameters) for tool in self._tools]

    def openai_responses_tools(self) -> list[dict[str, Any]]:
        """Tool definitions for the OpenAI Responses API."""
        return [
            openai_responses_tool(tool.name, tool.description, tool.parameters)
            for tool in self._tools
        ]

    def anthropic_tools(self) -> list[dict[str, Any]]:
        """Tool definitions for the Anthropic Messages API."""
        return [
            anthropic_tool(tool.name, tool.description, tool.parameters) for tool in self._tools
        ]

    def instructions(self) -> str:
        """A short system-prompt section explaining how to use the tools together."""
        config = self.querypilot.config
        lines = [
            f"You have read-only, policy-checked access to a {config.dialect} database.",
            "Workflow:",
            (
                f"1. Call {self.name_of('list_tables')} to see the tables "
                f"(or {self.name_of('search_schema')} with keywords on a large database)."
            ),
            (
                f"2. Call {self.name_of('describe_table')} on the relevant tables to get exact column "
                "names, join keys and example values. Never guess column names."
            ),
            f"3. Call {self.name_of('run_sql')} with a single SELECT statement.",
            (
                f"Results are capped at {config.max_rows} rows, so aggregate in SQL "
                "(COUNT, SUM, GROUP BY) instead of fetching raw rows."
            ),
            "If a tool returns ok=false, read the error and hints, fix the call, and retry.",
        ]
        if config.hints:
            lines.append("Business rules:")
            lines.extend(f"- {hint}" for hint in config.hints)
        return "\n".join(lines)

    # ------------------------------------------------------------------ dispatch

    def dispatch(
        self, name: str, arguments: Mapping[str, Any] | str | None = None
    ) -> dict[str, Any]:
        """Run a tool by name. ``arguments`` may be a dict or a JSON string. Never raises."""
        tool = self.get(name)
        if tool is None:
            return _error(
                f"Unknown tool: {name}",
                [f"Available tools: {', '.join(self.tool_names)}"],
            )
        try:
            parsed = _parse_arguments(arguments)
        except (TypeError, ValueError) as exc:
            return _error(str(exc), ["Pass the tool arguments as a JSON object."])
        try:
            return tool.handler(parsed)
        except _ArgumentError as exc:
            return _error(str(exc), [f"{tool.name} expects: {_describe_params(tool.parameters)}"])
        except Exception as exc:  # noqa: BLE001 - every failure becomes a payload
            return _error(self._redact(clean_error(f"{type(exc).__name__}: {exc}")))

    def dispatch_json(self, name: str, arguments: Mapping[str, Any] | str | None = None) -> str:
        """``dispatch`` serialised to a JSON string, for frameworks that expect text."""
        return to_json(self.dispatch(name, arguments))

    # ------------------------------------------------------------------ tools

    def _build(self, key: str) -> QueryPilotTool:
        config = self.querypilot.config
        description, properties = _tool_specs(self.name_of, config.dialect, config.max_rows)[key]
        handlers: dict[str, ToolHandler] = {
            "list_tables": self._list_tables,
            "describe_table": self._describe_table,
            "search_schema": self._search_schema,
            "validate_sql": self._validate_sql,
            "run_sql": self._run_sql,
            "ask_database": self._ask_database,
        }
        return QueryPilotTool(
            key=key,
            name=self.name_of(key),
            title=_TITLES[key],
            description=description,
            parameters=_object_schema(properties),
            handler=handlers[key],
        )

    def _list_tables(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tables = self.visible_schema().tables
        entries = []
        for table in tables:
            entry: dict[str, Any] = {"name": table.name}
            if table.schema_name:
                entry["schema"] = table.schema_name
            if table.is_view:
                entry["is_view"] = True
            if table.comment:
                entry["comment"] = _truncate(table.comment, 200)
            entry["columns"] = len(table.columns)
            entries.append(entry)
        kept = _fit(entries, self.max_payload_chars)
        payload: dict[str, Any] = {"ok": True, "tables": kept, "count": len(entries)}
        if len(kept) < len(entries):
            payload["truncated"] = True
            payload["note"] = (
                f"Only {len(kept)} of {len(entries)} tables are listed. "
                f"Use {self.name_of('search_schema')} with keywords to find the rest."
            )
        return payload

    def _describe_table(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requested = _table_names_argument(arguments)
        visible = self.visible_schema()
        found_names: list[str] = []
        unknown: list[str] = []
        for name in requested:
            table = visible.get_table(name)
            if table is None:
                unknown.append(name)
            elif table.name not in found_names:
                found_names.append(table.name)

        hints = _table_suggestions(unknown, visible, self.name_of("list_tables"))
        if not found_names:
            return _error(f"Unknown table: {', '.join(unknown) or '(none given)'}", hints)

        described = []
        for table in self.querypilot.table_details(found_names):
            visible_table = visible.get_table(table.name)
            if visible_table is not None:
                described.append(self._describe(table, visible_table, visible))
        payload: dict[str, Any] = {"ok": True, "tables": _fit(described, self.max_payload_chars)}
        if unknown:
            payload["unknown_tables"] = unknown
            payload["hints"] = hints
        return payload

    def _search_schema(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = _string_argument(arguments, "query")
        visible = self.visible_schema()
        matches = []
        for match in self.querypilot.search_schema(query):
            table = visible.get_table(match.table)
            if table is None:
                continue
            # A match lists every column that matched, comma-separated.
            named = [name.strip() for name in (match.column or "").split(",") if name.strip()]
            columns = [name for name in named if table.get_column(name) is not None]
            if named and not columns:
                continue
            entry: dict[str, Any] = {"table": match.table}
            if columns:
                entry["columns"] = columns
            entry["reason"] = match.reason
            matches.append(entry)
        payload: dict[str, Any] = {"ok": True, "matches": matches[:25]}
        if not matches:
            payload["note"] = (
                f"No matches. Call {self.name_of('list_tables')} to browse the schema."
            )
        return payload

    def _validate_sql(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sql = _string_argument(arguments, "sql", aliases=("query",))
        validation = self.querypilot.validate_sql(sql)
        errors = self._public_errors(validation.errors)
        shown_sql, row_note = _agent_sql(sql, validation.rewritten_sql, validation.access_policy)
        payload: dict[str, Any] = {
            "ok": validation.valid,
            "sql": shown_sql,
            "errors": errors,
            "hints": self._hints(errors, sql),
            "warnings": [clean_error(warning) for warning in validation.warnings],
        }
        if row_note:
            payload["note"] = row_note
        return payload

    def _run_sql(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sql = _string_argument(arguments, "sql", aliases=("query",))
        qp = self.querypilot
        precheck = qp.validator.validate(sql, qp.get_schema())
        try:
            # execute_sql validates again, writes the audit record and applies masking.
            result = qp.execute_sql(sql)
        except ValueError as exc:
            errors = self._public_errors(precheck.errors or [str(exc).removeprefix("SQL validation failed: ")])
            return _error(f"SQL validation failed: {'; '.join(errors)}", self._hints(errors, sql))
        except Exception as exc:  # noqa: BLE001 - database errors go back to the model
            # Cleaned, with stored values redacted, plus "did you mean" hints.
            lines = describe_db_error(exc, self.visible_schema(), sql)
            return _error(lines[0], lines[1:])

        shown_sql, row_note = _agent_sql(sql, result.sql, result.access_policy)
        payload: dict[str, Any] = {"ok": True, "sql": shown_sql}
        payload.update(self._rows_payload(result.rows, budget_used=len(shown_sql or "")))
        payload["warnings"] = [clean_error(warning) for warning in precheck.warnings]
        _add_note(payload, row_note)
        return _with_note_last(payload)

    def _ask_database(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = _string_argument(arguments, "question")
        try:
            answer = self.querypilot.ask(question)
        except ValueError as exc:
            message = clean_error(str(exc))
            hints = [
                (
                    f"Try {self.name_of('describe_table')} and write the SQL yourself with "
                    f"{self.name_of('run_sql')}."
                )
            ]
            return _error(message, hints)
        shown_sql, row_note = _agent_sql(
            answer.generated_sql or answer.sql, answer.sql, answer.access_policy
        )
        payload: dict[str, Any] = {"ok": True, "sql": shown_sql}
        payload.update(self._rows_payload(answer.rows, budget_used=len(shown_sql or "")))
        payload["explanation"] = answer.explanation
        payload["warnings"] = [clean_error(warning) for warning in answer.validation.warnings]
        _add_note(payload, row_note)
        return _with_note_last(payload)

    # ------------------------------------------------------------------ helpers

    def _rows_payload(self, rows: list[dict[str, Any]], *, budget_used: int) -> dict[str, Any]:
        columns = list(rows[0].keys()) if rows else []
        budget = max(self.max_payload_chars - budget_used - 600, 0)
        shown: list[list[Any]] = []
        used = 0
        cells_cut = False
        for row in rows:
            values = []
            for column in columns:
                value, cut = _jsonable(row.get(column), self.max_cell_chars)
                cells_cut = cells_cut or cut
                values.append(value)
            size = len(to_json(values)) + 1
            if used + size > budget:
                break
            shown.append(values)
            used += size

        max_rows = self.querypilot.config.max_rows
        row_count = len(rows)
        notes = []
        hit_limit = row_count >= max_rows
        if hit_limit:
            notes.append(
                f"Results stopped at the {max_rows}-row limit, so more rows may exist. "
                "Aggregate (COUNT, SUM, GROUP BY) or add filters instead of paging through rows."
            )
        if len(shown) < row_count:
            notes.append(
                f"Only the first {len(shown)} of {row_count} rows are shown to keep the result "
                f"under {self.max_payload_chars} characters. Select fewer columns or aggregate."
            )
        if cells_cut:
            notes.append(f"Long values were shortened to {self.max_cell_chars} characters.")

        payload: dict[str, Any] = {
            "columns": columns,
            "rows": shown,
            "row_count": row_count,
            "truncated": hit_limit or len(shown) < row_count or cells_cut,
        }
        if len(shown) < row_count:
            payload["rows_shown"] = len(shown)
        if notes:
            payload["note"] = " ".join(notes)
        return payload

    def _describe(
        self, table: TableSchema, visible_table: TableSchema, visible: DatabaseSchema
    ) -> dict[str, Any]:
        masked = self._masked_columns(table.name)
        visible_columns = {column.name.lower() for column in visible_table.columns}
        entry: dict[str, Any] = {"name": table.name}
        if table.schema_name:
            entry["schema"] = table.schema_name
        if table.is_view:
            entry["is_view"] = True
        if table.comment:
            entry["comment"] = table.comment
        entry["columns"] = [
            _describe_column(column, masked.get(column.name.lower()), self.max_cell_chars)
            for column in table.columns
            if column.name.lower() in visible_columns
        ]
        foreign_keys = []
        for key in table.foreign_keys:
            referred = visible.get_table(key.referred_table)
            if referred is None:
                continue
            if any(column.lower() not in visible_columns for column in key.columns):
                continue
            if any(referred.get_column(column) is None for column in key.referred_columns):
                continue
            foreign_keys.append(
                {
                    "columns": key.columns,
                    "references": f"{key.referred_table}({', '.join(key.referred_columns)})",
                }
            )
        if foreign_keys:
            entry["foreign_keys"] = foreign_keys
        return entry

    def visible_schema(self) -> DatabaseSchema:
        """The schema minus tables, columns and foreign keys the configuration hides."""
        schema = self.querypilot.get_schema()
        visible = {table.name.lower(): table for table in schema.tables if self._table_visible(table)}
        tables = []
        for table in visible.values():
            columns = [column for column in table.columns if self._column_visible(table, column)]
            kept = {column.name.lower() for column in columns}
            foreign_keys = []
            for key in table.foreign_keys:
                referred = visible.get(key.referred_table.split(".")[-1].lower())
                if referred is None or any(column.lower() not in kept for column in key.columns):
                    continue
                referred_columns = {
                    column.name.lower()
                    for column in referred.columns
                    if self._column_visible(referred, column)
                }
                if all(column.lower() in referred_columns for column in key.referred_columns):
                    foreign_keys.append(key)
            tables.append(table.model_copy(update={"columns": columns, "foreign_keys": foreign_keys}))
        return schema.model_copy(update={"tables": tables})

    def _table_visible(self, table: TableSchema) -> bool:
        config = self.querypilot.config
        names = _table_keys(table)
        if config.blocked_tables and names & _lower(config.blocked_tables):
            return False
        return not (config.allowed_tables and not names & _lower(config.allowed_tables))

    def _column_visible(self, table: TableSchema, column: ColumnSchema) -> bool:
        policy = self.querypilot.config.access_policy
        names = _table_keys(table)
        column_name = column.name.lower()
        for table_name, columns in policy.blocked_columns.items():
            if table_name.lower() in names and column_name in _lower(columns):
                return False
        for table_name, columns in policy.allowed_columns.items():
            if table_name.lower() in names and column_name not in _lower(columns):
                return False
        return True

    def _masked_columns(self, table_name: str) -> dict[str, str]:
        rules = self.querypilot.config.access_policy.masking_rules
        lowered = table_name.lower()
        return {
            column.lower(): rule.mode
            for name, columns in rules.items()
            if name.lower().split(".")[-1] == lowered
            for column, rule in columns.items()
        }

    def _hints(self, errors: list[str], sql: str | None = None) -> list[str]:
        if not errors:
            return []
        try:
            return list(repair_hints(errors, self.visible_schema(), sql))
        except Exception:  # noqa: BLE001 - hints are best effort
            return []

    def _public_errors(self, errors: list[str]) -> list[str]:
        """Validation errors without the names of tables and columns the policy hides."""
        return public_errors(errors, self.name_of("describe_table"))

    def _redact(self, message: str) -> str:
        try:
            return redact_values(message, self.visible_schema())
        except Exception:  # noqa: BLE001 - fall back to a message without details
            return "The tool failed with an unexpected error."


def default_tool_definitions() -> list[tuple[str, str, dict[str, Any]]]:
    """(name, description, parameters) for every tool, without database specifics."""
    specs = _tool_specs(lambda key: key, None, None)
    return [(key, specs[key][0], _object_schema(specs[key][1])) for key in TOOL_KEYS]


def _tool_specs(
    n: Callable[[str], str], dialect: str | None, max_rows: int | None
) -> dict[str, tuple[str, dict[str, Any]]]:
    statement = f"A single {dialect} SELECT statement." if dialect else "A single SELECT statement."
    flavour = f" ({dialect} dialect)" if dialect else ""
    limit = (
        f"at most {max_rows} rows come back"
        if max_rows
        else "the number of rows returned is capped"
    )
    return {
        "list_tables": (
            (
                "List the tables and views you can query, with their descriptions and column "
                f"counts. Call this first, then call {n('describe_table')} on the tables that look "
                f"relevant before writing SQL. On a large database, {n('search_schema')} is faster."
            ),
            {},
        ),
        "describe_table": (
            (
                "Show the columns (type, primary key, nullability, description, example values), "
                "foreign keys and description of one or more tables. Call it before writing SQL "
                "so you use real column names, correct join keys and literal values that actually "
                "occur in the data. Pass every table you plan to join in one call."
            ),
            {
                "table_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Table names as returned by {n('list_tables')}.",
                }
            },
        ),
        "search_schema": (
            (
                "Find tables and columns whose names or descriptions match keywords, e.g. "
                f"'revenue' or 'customer email'. Use it instead of {n('list_tables')} on large "
                f"databases, then call {n('describe_table')} on the matching tables."
            ),
            {"query": {"type": "string", "description": "Keywords to look for."}},
        ),
        "validate_sql": (
            (
                "Check a SQL query against the schema and the safety policy without running it. "
                "Returns the SQL that would run (with the row limit applied), errors with fix "
                f"hints, and warnings. {n('run_sql')} validates too, so only use this when you "
                "want to check a query without executing it."
            ),
            {"sql": {"type": "string", "description": statement}},
        ),
        "run_sql": (
            (
                f"Run one read-only SQL SELECT statement{flavour} and return the columns and rows. "
                "The query is validated first: only reads of allowed tables and columns run, and "
                f"{limit}, so aggregate (COUNT, SUM, GROUP BY) or filter in SQL rather than fetching "
                "raw rows. On failure the result has ok=false with an error and hints; fix the SQL "
                "and call again."
            ),
            {"sql": {"type": "string", "description": statement}},
        ),
        "ask_database": (
            (
                "Answer a natural-language question: QueryPilot writes, validates and runs the "
                "SQL and returns it with the rows. Good for simple questions; for anything "
                f"subtle, use {n('list_tables')}, {n('describe_table')} and {n('run_sql')} "
                "so you control the query."
            ),
            {"question": {"type": "string", "description": "The question to answer."}},
        ),
    }


class _ArgumentError(ValueError):
    pass


def to_json(payload: Any) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))


def _error(message: str, hints: list[str] | None = None) -> dict[str, Any]:
    return {"ok": False, "error": message, "hints": list(hints or [])}


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _describe_params(parameters: dict[str, Any]) -> str:
    properties = parameters.get("properties", {})
    if not properties:
        return "no arguments"
    return ", ".join(f"{name} ({spec.get('type', 'any')})" for name, spec in properties.items())


def _parse_arguments(arguments: Mapping[str, Any] | str | None) -> dict[str, Any]:
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        if not arguments.strip():
            return {}
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Tool arguments are not valid JSON: {exc.msg}") from exc
    if not isinstance(arguments, Mapping):
        raise TypeError("Tool arguments must be a JSON object.")
    return dict(arguments)


def _string_argument(arguments: dict[str, Any], name: str, aliases: tuple[str, ...] = ()) -> str:
    for key in (name, *aliases):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise _ArgumentError(f"Missing required argument '{name}'.")


def _table_names_argument(arguments: dict[str, Any]) -> list[str]:
    value = next(
        (
            arguments[key]
            for key in ("table_names", "table_name", "tables", "table")
            if key in arguments
        ),
        None,
    )
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise _ArgumentError("Missing required argument 'table_names'.")
    names = [str(item).strip() for item in value if str(item).strip()]
    if not names:
        raise _ArgumentError("Missing required argument 'table_names'.")
    return names


def _table_suggestions(unknown: list[str], schema: DatabaseSchema, list_tool: str) -> list[str]:
    names = [table.name for table in schema.tables]
    hints = []
    for name in unknown:
        close = difflib.get_close_matches(name.split(".")[-1], names, n=3, cutoff=0.6)
        if close:
            hints.append(f"Unknown table '{name}'. Did you mean: {', '.join(close)}?")
    if unknown and not hints:
        hints.append(f"Call {list_tool} to see the tables you can use.")
    return hints


def _describe_column(column: ColumnSchema, mask_mode: str | None, max_chars: int) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": column.name, "type": column.type, "nullable": column.nullable}
    if column.primary_key:
        entry["primary_key"] = True
    if column.comment:
        entry["comment"] = column.comment
    if mask_mode is not None:
        entry["masked"] = mask_mode
    elif column.sample_values:
        entry["sample_values"] = [
            _truncate(str(value), min(max_chars, 100)) for value in column.sample_values
        ]
        if column.sample_values_complete:
            entry["all_values"] = True
    return entry


def _table_keys(table: TableSchema) -> set[str]:
    names = {table.name.lower()}
    if table.schema_name:
        names.add(f"{table.schema_name}.{table.name}".lower())
    return names


def _lower(values: list[str]) -> set[str]:
    return {value.lower() for value in values}


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def _jsonable(value: Any, max_chars: int) -> tuple[Any, bool]:
    """Convert a database value to a JSON-safe value, shortening long text."""
    if value is None or isinstance(value, bool | int):
        return value, False
    if isinstance(value, float):
        return (value if math.isfinite(value) else str(value)), False
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value), False
        return (int(value) if value == value.to_integral_value() else float(value)), False
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat(), False
    if isinstance(value, dt.timedelta):
        return value.total_seconds(), False
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        encoded = base64.b64encode(raw[:max_chars]).decode("ascii")
        return f"<{len(raw)} bytes base64:{encoded}>", len(raw) > max_chars
    if isinstance(value, UUID):
        return str(value), False
    if isinstance(value, dict | list | tuple):
        text = to_json(value)
        if len(text) <= max_chars:
            return json.loads(text), False
        return _truncate(text, max_chars), True
    text = value if isinstance(value, str) else str(value)
    if len(text) > max_chars:
        return _truncate(text, max_chars), True
    return text, False


def _fit(items: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    kept = []
    used = 0
    for item in items:
        size = len(to_json(item)) + 1
        if kept and used + size > max_chars:
            break
        kept.append(item)
        used += size
    return kept


def _agent_sql(
    sql: str, rewritten_sql: str | None, access_policy: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """The SQL to show the agent, and a note when row-level filters were applied.

    The rewritten SQL shows the row limit, but with row filters it would also
    reveal the filter predicates, so the agent then sees its own SQL and a note;
    the rewritten SQL stays in the audit record.
    """
    if rewritten_sql is None:
        return None, None
    if access_policy.get("row_filters"):
        return sql.strip(), ROW_FILTER_NOTE
    return rewritten_sql, None


def _add_note(payload: dict[str, Any], note: str | None) -> None:
    if note:
        existing = payload.get("note")
        payload["note"] = f"{note} {existing}" if existing else note


def _with_note_last(payload: dict[str, Any]) -> dict[str, Any]:
    note = payload.pop("note", None)
    if note:
        payload["note"] = note
    return payload
