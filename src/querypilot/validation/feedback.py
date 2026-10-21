"""Turn validation and database errors into feedback a model can act on.

Execution-feedback repair is one of the strongest text-to-SQL levers (CHESS
-6.80 EX without it, MAC-SQL +4.63, CHASE-SQL +3.78), and it works best when the
feedback says what to do next: the closest real column names, which tables
hold an ambiguous column, that a restricted column must be left out rather than
retried, or which foreign keys join the tables.

Pass the schema the model is allowed to see (see
``querypilot.schema.pruning.visible_schema``) so hints never suggest a hidden
table or column. Two more guards keep agent-facing text from leaking what the
access policy hides: ``public_errors`` replaces policy errors that name hidden
tables or columns with one generic message, and ``redact_values`` removes
quoted values (``could not convert string 'alice@x.com'``) from database errors.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

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
# Quoted text in an error: '...' or "...", not an apostrophe inside a word, up to
# the matching quote that ends a word (values may contain quotes) or the end.
_QUOTED = re.compile(r"(?<![\w'\"])(['\"])(.*?)(?:\1(?![\w'\"])|\Z)", re.DOTALL)
_NAME_PART = re.compile(r"[\w$]+")
REDACTED = "<redacted>"

_RESTRICTED_PREFIXES = (
    "Column is blocked by access policy:",
    "Column is not allowed by access policy:",
    "Blocked table referenced:",
    "Table is not in allowed_tables:",
)
INVALID_ROW_FILTER_ERROR = (
    "A row-level filter in the access policy could not be applied to this query; "
    "ask the operator to fix the QueryPilot configuration."
)

TIMEOUT_HINT = (
    "The query took too long and was stopped; simplify it (filter early, avoid "
    "correlated subqueries and unnecessary joins, aggregate before joining)."
)


def clean_error(message: str) -> str:
    """Strip terminal escape codes and surrounding whitespace from an error message."""
    return _ANSI_ESCAPE.sub("", message).strip()


def restricted_error(schema_source: str = "the schema") -> str:
    """The generic error shown instead of a policy error that names a hidden object."""
    return (
        "The query uses a column or table that is restricted by the access policy; "
        f"use only the columns shown by {schema_source} (avoid SELECT *)."
    )


def is_restricted_error(error: str) -> bool:
    """True for validator errors that name a table or column the access policy hides."""
    return clean_error(error).startswith(_RESTRICTED_PREFIXES)


def public_errors(errors: list[str], schema_source: str = "the schema") -> list[str]:
    """Errors safe to show an agent or a model, cleaned and de-duplicated.

    Access-policy errors name the hidden table or column (``Column is blocked
    by access policy: customers.revenue``), which the policy-filtered schema
    never shows, so they become one generic message. Invalid row-filter errors
    would echo the filter predicate, so they become a generic message too. Keep
    the original errors for operators (audit records, ``ValidationResult``).
    """
    public: list[str] = []
    for raw in errors:
        error = clean_error(raw)
        if is_restricted_error(error):
            error = restricted_error(schema_source)
        elif error.startswith("Invalid row filter"):
            error = INVALID_ROW_FILTER_ERROR
        if error not in public:
            public.append(error)
    return public


def redact_values(
    message: str,
    schema: DatabaseSchema,
    sql: str | None = None,
    keep: Iterable[str] = (),
) -> str:
    """Replace quoted text in a database error unless it is an identifier or the query's own text.

    Errors such as ``invalid input syntax for type integer: "alice@x.com"`` or
    ``Could not convert string 'alice@x.com' to INT32`` echo stored values,
    including masked or row-filtered ones. Quoted text is kept only when it is
    a table or column name of ``schema`` (optionally qualified) or a token of
    ``sql`` (the query as the caller wrote it, so its own names and literals
    survive), or is listed in ``keep``; everything else becomes ``'<redacted>'``.
    """
    known = {name.lower() for name in keep}
    known.update(table.name.lower() for table in schema.tables)
    known.update(column.name.lower() for table in schema.tables for column in table.columns)
    known.update(_sql_tokens(sql, schema.dialect))

    def replace(match: re.Match[str]) -> str:
        quote, content = match.group(1), match.group(2)
        bare = content.strip().lower()
        parts = [part.strip("\"`[] ") for part in bare.split(".")]
        # A qualified name counts when its last part is known (db.schema.table).
        qualified = parts[-1] in known and all(_NAME_PART.fullmatch(part) for part in parts)
        if not bare or bare in known or qualified:
            return match.group(0)
        return f"{quote}{REDACTED}{quote}"

    return _QUOTED.sub(replace, message)


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
    PostgreSQL's / DuckDB's ``LINE n:`` / caret echo lines are dropped. Quoted
    values are redacted (see ``redact_values``), so pass the query as the
    caller wrote it as ``sql``, not the policy-rewritten one.
    """
    message = clean_db_message(exc)
    # Names in "column x does not exist"-style errors come from the query, not the data.
    names = [
        match.group(1).strip("\"`[]")
        for _, pattern in _DB_PATTERNS
        if (match := pattern.search(message))
    ]
    lines = [f"Database error: {redact_values(message, schema, sql, keep=names)}"]
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
    # Policy errors name objects the visible schema hides, so the hints don't.
    if error.startswith(("Column is blocked by access policy:", "Column is not allowed by access policy:")):
        return (
            "A column the query uses is restricted for this user; answer using only the "
            "columns in the schema (list them explicitly, do not use SELECT *)."
        )
    if error.startswith(("Blocked table referenced:", "Table is not in allowed_tables:")):
        return (
            "A table the query uses is restricted for this user; answer using the "
            "tables in the schema, or return the closest answer they support."
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


def _sql_tokens(sql: str | None, dialect: str) -> set[str]:
    """Lower-cased token texts of ``sql`` (names, keywords and literals), best effort."""
    if not sql:
        return set()
    try:
        tokens = sqlglot.tokenize(sql, read=_SQLGLOT_DIALECTS.get(dialect, dialect))
    except Exception:  # noqa: BLE001 - redaction then keeps schema names only
        return set()
    return {token.text.lower() for token in tokens if token.text}


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
