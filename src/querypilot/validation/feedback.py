"""Turn validation and database errors into feedback a model can act on.

Execution-feedback repair is one of the strongest text-to-SQL levers (CHESS
-6.80 EX without it, MAC-SQL +4.63, CHASE-SQL +3.78), and it works best when the
feedback says what to do next: the closest real column names, which tables
hold an ambiguous column, that a restricted column must be left out rather than
retried, or which foreign keys join the tables.

Pass the schema the model is allowed to see (see
``querypilot.schema.pruning.visible_schema``) so hints never suggest a hidden
table or column.
"""
from __future__ import annotations

import difflib
import re

import sqlglot
from sqlglot import exp

from querypilot.core.types import DatabaseSchema

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SQLA_BACKGROUND = re.compile(r"\(Background on this error at:[^)]*\)", re.IGNORECASE)
_SQLA_PREFIX = re.compile(r"^\(([\w.]+)\)\s*")
_MAX_LISTED = 30
_SQLGLOT_DIALECTS = {"postgresql": "postgres", "mssql": "tsql", "mariadb": "mysql"}

# Database error text -> (kind, name). Covers SQLite, PostgreSQL, MySQL, DuckDB.
_DB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("column", re.compile(r"no such column:\s*([\w.\"`\[\]]+)", re.IGNORECASE)),
    ("column", re.compile(r"column \"?([\w.]+)\"? does not exist", re.IGNORECASE)),
    ("column", re.compile(r"unknown column '([^']+)'", re.IGNORECASE)),
    ("column", re.compile(r"referenced column \"?([\w.]+)\"? not found", re.IGNORECASE)),
    ("table", re.compile(r"no such table:\s*([\w.\"`\[\]]+)", re.IGNORECASE)),
    ("table", re.compile(r"relation \"?([\w.]+)\"? does not exist", re.IGNORECASE)),
    ("table", re.compile(r"table '([^']+)' doesn't exist", re.IGNORECASE)),
    ("table", re.compile(r"table with name \"?([\w.]+)\"? does not exist", re.IGNORECASE)),
    ("ambiguous", re.compile(r"ambiguous column name:\s*([\w.]+)", re.IGNORECASE)),
    ("ambiguous", re.compile(r"column reference \"?([\w.]+)\"? is ambiguous", re.IGNORECASE)),
]


_TIMEOUT = re.compile(
    r"\binterrupted\b|statement timeout|canceling statement|maximum statement execution time|"
    r"query timeout|timed out",
    re.IGNORECASE,
)
TIMEOUT_HINT = (
    "The query took too long and was stopped; simplify it (filter early, avoid "
    "correlated subqueries and unnecessary joins, aggregate before joining)."
)


def clean_error(message: str) -> str:
    """Strip terminal escape codes and surrounding whitespace from an error message."""
    return _ANSI_ESCAPE.sub("", message).strip()


def repair_hints(
    errors: list[str],
    schema: DatabaseSchema,
    sql: str | None = None,
) -> list[str]:
    """Suggest fixes for validation errors, one hint per actionable error.

    ``sql`` (the query that failed) is optional; when given, hints can name the
    columns of the tables the query actually uses and the foreign keys between
    them.
    """
    used_tables, aliases = _query_tables(sql, schema)
    hints: list[str] = []
    for raw in errors:
        error = clean_error(raw)
        hint = _hint_for(error, schema, used_tables, aliases, sql)
        if hint and hint not in hints:
            hints.append(hint)
    return hints


def describe_db_error(
    exc: BaseException,
    schema: DatabaseSchema,
    sql: str | None = None,
) -> list[str]:
    """Feedback lines for a database execution error: the cleaned message, then hints.

    The DBAPI message is unwrapped from SQLAlchemy's decorations (the
    ``(sqlite3.OperationalError)`` prefix, the echoed ``[SQL: ...]`` and
    ``[parameters: ...]`` blocks, and the "Background on this error" link) and
    PostgreSQL's ``LINE n:`` / caret echo lines are dropped.
    """
    message = clean_db_message(exc)
    lines = [f"Database error: {message}"]
    if _TIMEOUT.search(message):
        return [*lines, TIMEOUT_HINT]
    used_tables, aliases = _query_tables(sql, schema)
    for kind, pattern in _DB_PATTERNS:
        match = pattern.search(message)
        if not match:
            continue
        name = match.group(1).strip("\"`[]")
        error = {
            "column": f"Unknown column: {name}",
            "table": f"Unknown table: {name}",
            "ambiguous": f"Ambiguous column: {name}",
        }[kind]
        hint = _hint_for(error, schema, used_tables, aliases, sql)
        if hint:
            lines.append(hint)
        break
    return lines


def clean_db_message(exc: BaseException) -> str:
    original = getattr(exc, "orig", None)
    text = str(original) if original is not None and str(original) else str(exc)
    text = clean_error(text)
    text = _SQLA_BACKGROUND.sub("", text)
    # SQLAlchemy appends "[SQL: ...]", "[parameters: ...]" and a docs link; the
    # echoed SQL can span lines, so drop everything from the first block on.
    echo = text.find("[SQL:")
    if echo != -1:
        text = text[:echo]
    text = _SQLA_PREFIX.sub("", text.strip())
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("LINE ") or set(stripped) <= {"^", " "}:
            continue
        kept.append(stripped)
    return " ".join(kept) or type(exc).__name__


# --------------------------------------------------------------------------- hints


def _hint_for(
    error: str,
    schema: DatabaseSchema,
    used_tables: list[str],
    aliases: dict[str, str],
    sql: str | None,
) -> str | None:
    if error.startswith("Unknown column:"):
        return _unknown_column_hint(_after_colon(error), schema, used_tables, aliases, sql)
    if error.startswith("Unknown table qualifier:"):
        qualifier = _after_colon(error)
        names = sorted(set(aliases)) or used_tables
        return (
            f"'{qualifier}' is not a table or alias in the FROM clause; "
            f"use one of: {_listing(names)}."
            if names
            else f"'{qualifier}' is not a table or alias in the FROM clause."
        )
    if error.startswith("Unknown table:"):
        return _unknown_table_hint(_after_colon(error), schema)
    if error.startswith("Ambiguous column:"):
        return _ambiguous_hint(_after_colon(error), schema, used_tables)
    if error.startswith(("Column is blocked by access policy:", "Column is not allowed by access policy:")):
        target = _after_colon(error)
        return (
            f"{target} is restricted for this user; answer without it "
            "(do not select, filter, join or sort on it, and do not use SELECT *)."
        )
    if error.startswith(("Blocked table referenced:", "Table is not in allowed_tables:")):
        return (
            f"Table {_after_colon(error)} is restricted for this user; answer using the "
            "other tables in the schema, or return the closest answer they support."
        )
    if error.startswith("Potential Cartesian join"):
        return _cartesian_hint(schema, used_tables)
    if error.startswith("SELECT * is disabled"):
        return "List the needed columns explicitly instead of SELECT *."
    if error.startswith(("SQL function is blocked", "SQL function is not allowed")):
        return f"Do not use {_after_colon(error)}; rewrite the query without that function."
    if error.startswith(("Only SELECT", "SQL contains a non-read-only")):
        return "Write a single read-only SELECT statement."
    if error.startswith("SQL parse error"):
        return "The SQL could not be parsed; return one complete, syntactically valid statement."
    return None


def _unknown_column_hint(
    name: str,
    schema: DatabaseSchema,
    used_tables: list[str],
    aliases: dict[str, str],
    sql: str | None,
) -> str:
    qualifier, _, column = name.rpartition(".")
    column = column.strip("\"`[]")
    qualified = _all_columns(schema)
    bare = {}
    for table_name, column_name in qualified:
        bare.setdefault(column_name.lower(), []).append(f"{table_name}.{column_name}")

    close = difflib.get_close_matches(column.lower(), list(bare), n=4, cutoff=0.6)
    suggestions: list[str] = []
    for match in close:
        suggestions.extend(bare[match])

    target_tables = _target_tables(column, qualifier, aliases, used_tables, sql)
    # Suggestions from the tables the query uses win; others are a fallback.
    if target_tables:
        in_scope = [s for s in suggestions if s.split(".")[0].lower() in target_tables]
        suggestions = in_scope or suggestions

    parts = [f"Column '{column}' does not exist"]
    if suggestions:
        parts[0] += f"; did you mean {_listing(suggestions[:3], quote=False)}?"
    else:
        parts[0] += "."
    for table_name in target_tables[:2]:
        table = schema.get_table(table_name)
        if table is not None:
            parts.append(f"Columns of {table.name}: {_listing([c.name for c in table.columns])}.")
    if not target_tables and not suggestions:
        parts.append(f"Tables: {_listing(sorted(t.name for t in schema.tables))}.")
    return " ".join(parts)


def _unknown_table_hint(name: str, schema: DatabaseSchema) -> str:
    bare = name.split(".")[-1].strip("\"`[]")
    names = sorted(table.name for table in schema.tables)
    lowered = {table_name.lower(): table_name for table_name in names}
    close = [lowered[m] for m in difflib.get_close_matches(bare.lower(), list(lowered), n=3, cutoff=0.5)]
    hint = f"Table '{bare}' does not exist"
    hint += f"; did you mean {_listing(close, quote=False)}?" if close else "."
    return f"{hint} Available tables: {_listing(names)}."


def _ambiguous_hint(name: str, schema: DatabaseSchema, used_tables: list[str]) -> str:
    column = name.split(".")[-1]
    owners = [
        table.name
        for table in schema.tables
        if table.get_column(column) is not None
        and (not used_tables or table.name.lower() in used_tables)
    ]
    if not owners:
        return f"Qualify '{column}' with its table name or alias."
    examples = ", ".join(f"{owner}.{column}" for owner in owners[:4])
    return f"'{column}' exists in {', '.join(owners[:4])}; qualify it, e.g. {examples}."


def _cartesian_hint(schema: DatabaseSchema, used_tables: list[str]) -> str:
    scope = set(used_tables)
    joins: list[str] = []
    for table in schema.tables:
        if scope and table.name.lower() not in scope:
            continue
        for foreign_key in table.foreign_keys:
            referred = foreign_key.referred_table.split(".")[-1]
            if scope and referred.lower() not in scope:
                continue
            pairs = zip(foreign_key.columns, foreign_key.referred_columns, strict=False)
            joins.append(
                " AND ".join(f"{table.name}.{a} = {referred}.{b}" for a, b in pairs)
            )
    if joins:
        return "Add a join condition using the foreign keys: " + "; ".join(joins[:6]) + "."
    return "Add an explicit ON condition for every joined table (no comma joins without a WHERE link)."


def _target_tables(
    column: str,
    qualifier: str,
    aliases: dict[str, str],
    used_tables: list[str],
    sql: str | None,
) -> list[str]:
    if qualifier:
        resolved = aliases.get(qualifier.strip("\"`[]").lower())
        if resolved:
            return [resolved]
    if sql:
        try:
            parsed = sqlglot.parse_one(sql)
        except Exception:  # noqa: BLE001 - hints are best-effort
            parsed = None
        if parsed is not None:
            for node in parsed.find_all(exp.Column):
                if node.name.lower() == column.lower() and node.table:
                    resolved = aliases.get(node.table.lower())
                    if resolved:
                        return [resolved]
    return used_tables


def _query_tables(sql: str | None, schema: DatabaseSchema) -> tuple[list[str], dict[str, str]]:
    """Lower-cased names of known tables the query reads, and alias -> table."""
    if not sql:
        return [], {}
    try:
        parsed = sqlglot.parse_one(sql, read=_SQLGLOT_DIALECTS.get(schema.dialect, schema.dialect))
    except Exception:  # noqa: BLE001 - hints are best-effort
        try:
            parsed = sqlglot.parse_one(sql)
        except Exception:  # noqa: BLE001
            return [], {}
    if parsed is None:
        return [], {}
    names = schema.table_names
    tables: list[str] = []
    aliases: dict[str, str] = {}
    for node in parsed.find_all(exp.Table):
        name = node.name.lower()
        if name not in names:
            continue
        if name not in tables:
            tables.append(name)
        aliases[name] = name
        if node.alias:
            aliases[node.alias.lower()] = name
    return tables, aliases


def _all_columns(schema: DatabaseSchema) -> list[tuple[str, str]]:
    return [(table.name, column.name) for table in schema.tables for column in table.columns]


def _after_colon(error: str) -> str:
    return error.split(":", 1)[1].strip() if ":" in error else error


def _listing(items: list[str], quote: bool = False) -> str:
    shown = [f"'{item}'" if quote else item for item in items[:_MAX_LISTED]]
    text = ", ".join(shown)
    if len(items) > _MAX_LISTED:
        text += f", ... ({len(items) - _MAX_LISTED} more)"
    return text
