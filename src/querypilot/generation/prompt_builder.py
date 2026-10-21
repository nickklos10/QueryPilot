"""Build the prompts QueryPilot sends to an LLM SQL generator.

The schema is rendered as compact DDL-like text: column types, primary keys,
descriptions, a few real stored values per text column, and foreign-key lines.
Each of these measurably improves text-to-SQL accuracy (see
docs/research/developer-needs.md, "Quality levers"), as do business hints and an
explicit dialect. Repair prompts are worded gently: they report what happened
when the query was checked or run and allow the model to keep a correct query,
because "your query is wrong" wording makes strong models break good SQL.
"""
from __future__ import annotations

from pydantic import BaseModel
from sqlglot import exp

from querypilot.core.types import ColumnSchema, DatabaseSchema, TableSchema

_DIALECT_NAMES = {
    "sqlite": "SQLite",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "duckdb": "DuckDB",
    "snowflake": "Snowflake",
    "bigquery": "BigQuery",
    "redshift": "Redshift",
    "tsql": "SQL Server (T-SQL)",
    "mssql": "SQL Server (T-SQL)",
    "oracle": "Oracle",
    "trino": "Trino",
    "presto": "Presto",
    "databricks": "Databricks SQL",
    "spark": "Spark SQL",
    "clickhouse": "ClickHouse",
}

_SQLGLOT_DIALECTS = {"postgresql": "postgres", "mssql": "tsql", "mariadb": "mysql"}


class SQLGenerationPrompt(BaseModel):
    instructions: str
    user_prompt: str


def dialect_display_name(dialect: str) -> str:
    return _DIALECT_NAMES.get(dialect.lower(), dialect)


def build_sql_generation_prompt(
    question: str,
    schema: DatabaseSchema,
    max_rows: int,
    validation_errors: list[str] | None = None,
    previous_sql: str | None = None,
) -> SQLGenerationPrompt:
    """Build the system instructions and user prompt for one generation call.

    When ``previous_sql`` is given this is a repair call, and
    ``validation_errors`` carries the feedback lines QueryPilot collected when
    the previous query was validated or executed (errors, "did you mean"
    hints, a database error, or an empty-result note).
    """
    dialect = dialect_display_name(schema.dialect)
    instructions = (
        f"You write {dialect} SQL for QueryPilot. Write a single {dialect} SELECT "
        "statement (a WITH ... SELECT is fine) that answers the question. "
        "Generate SELECT-only SQL: no INSERT/UPDATE/DELETE, DDL, multiple statements, "
        "SQL comments or vendor-specific admin commands. "
        "Use only the tables and columns listed in the schema, and join tables on the "
        "listed foreign keys. When the question names a value, match the stored "
        "spelling shown in the 'one of' / 'e.g.' values. Follow the business rules when given. "
        f"The maximum row limit is {max_rows}; include a LIMIT no larger than that "
        "unless the query is an aggregate that returns one row. "
        'Return only JSON: {"sql": "<the SQL, nothing else>", "explanation": '
        '"<one short sentence>"}. Put no prose or markdown in the sql value.'
    )
    if previous_sql:
        instructions += (
            " You are reviewing a previous attempt: keep it if it is already correct, "
            "otherwise return a corrected query."
        )

    parts = [
        f"Question: {question}",
        "",
        f"Schema ({dialect}):",
        build_schema_context(schema),
    ]
    if schema.hints:
        parts.extend(["", "Business rules / hints:"])
        parts.extend(f"- {hint}" for hint in schema.hints)
    if previous_sql:
        parts.extend(["", "Previous SQL:", previous_sql.strip()])
        if validation_errors:
            parts.extend(
                [
                    "",
                    (
                        "The previous query may need changes. Here is what happened "
                        "when it was checked or run:"
                    ),
                ]
            )
            parts.extend(f"- {line}" for line in validation_errors)
        parts.extend(
            [
                "",
                (
                    "If the previous query is already correct, return it unchanged; "
                    "otherwise return a corrected query."
                ),
            ]
        )
    elif validation_errors:
        parts.extend(["", "Notes from checking an earlier query:"])
        parts.extend(f"- {line}" for line in validation_errors)
    return SQLGenerationPrompt(instructions=instructions, user_prompt="\n".join(parts))


def build_schema_context(schema: DatabaseSchema) -> str:
    """Render every table of ``schema`` as compact DDL-like text."""
    return "\n\n".join(render_table(table, schema.dialect) for table in schema.tables)


def render_table(table: TableSchema, dialect: str = "sqlite") -> str:
    kind = "VIEW" if table.is_view else "TABLE"
    header = f"CREATE {kind} {_ident(table.name, dialect)} ("
    if table.comment:
        header += f" -- {_one_line(table.comment)}"
    lines = [header]
    for index, column in enumerate(table.columns):
        separator = "," if index < len(table.columns) - 1 else ""
        lines.append(f"  {_render_column(column, dialect)}{separator}{_column_note(column)}")
    lines.append(")")
    for foreign_key in table.foreign_keys:
        lines.append(f"-- {_render_foreign_key(table.name, foreign_key)}")
    return "\n".join(lines)


def _render_column(column: ColumnSchema, dialect: str) -> str:
    text = f"{_ident(column.name, dialect)} {column.type}".rstrip()
    if column.primary_key:
        text += " PRIMARY KEY"
    return text


def _column_note(column: ColumnSchema) -> str:
    notes: list[str] = []
    if column.comment:
        notes.append(_one_line(column.comment))
    if column.sample_values:
        label = "one of" if column.sample_values_complete else "e.g."
        notes.append(f"{label} " + ", ".join(_quote_value(value) for value in column.sample_values))
    return f" -- {'; '.join(notes)}" if notes else ""


def _render_foreign_key(table_name: str, foreign_key) -> str:
    if len(foreign_key.columns) == 1 and len(foreign_key.referred_columns) == 1:
        return (
            f"{table_name}.{foreign_key.columns[0]} -> "
            f"{foreign_key.referred_table}.{foreign_key.referred_columns[0]}"
        )
    return (
        f"{table_name}({', '.join(foreign_key.columns)}) -> "
        f"{foreign_key.referred_table}({', '.join(foreign_key.referred_columns)})"
    )


def _ident(name: str, dialect: str) -> str:
    try:
        return exp.to_identifier(name).sql(dialect=_SQLGLOT_DIALECTS.get(dialect, dialect))
    except Exception:  # noqa: BLE001 - unknown dialects fall back to the raw name
        return name


def _quote_value(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _one_line(text: str) -> str:
    return " ".join(text.split())
