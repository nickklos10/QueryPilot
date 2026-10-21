"""Fetch a few distinct stored values per text column for the prompt.

Showing a model three real values per text column (``'Enterprise', 'SMB'``)
improves accuracy by about 3 EX because it filters on the stored spelling.
Sampling runs queries QueryPilot builds itself, so it applies the access policy
here instead of in the validator, and it is conservative:

* no sampling of blocked tables, tables outside ``allowed_tables``, or tables
  with a row filter (an unfiltered sample would leak rows the filter hides);
* no sampling of blocked, non-allowed or masked columns;
* no sampling of key/ID columns or columns whose names suggest secrets or
  contact details (passwords, tokens, emails, phone numbers, ...).

Values are fetched lazily, one table at a time, with a bounded scan, and any
database error just leaves the column without samples.
"""
from __future__ import annotations

import logging
import re
import threading
import weakref

from sqlglot import exp

from querypilot.connectors.base import BaseConnector
from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import ColumnSchema, TableSchema

logger = logging.getLogger(__name__)

# Rows scanned per column before DISTINCT, so sampling stays cheap on big tables.
SCAN_ROWS = 10_000
MAX_VALUE_CHARS = 40

_TEXT_TYPES = ("CHAR", "TEXT", "STRING", "CLOB", "ENUM", "CITEXT", "NAME")
_NON_TEXT_TYPES = ("UUID", "JSON", "XML", "BINARY", "BLOB", "BYTEA")
_SENSITIVE_NAME = re.compile(
    r"pass(word|wd)?|secret|token|api_?key|private|credential|salt|hash|ssn|"
    r"social_security|tax_?id|iban|card|cvv|email|e_mail|phone|mobile|address|"
    r"street|zip|postal|birth|dob|ip_?addr|first_?name|last_?name|full_?name|surname",
    re.IGNORECASE,
)
_SQLGLOT_DIALECTS = {"postgresql": "postgres", "mssql": "tsql", "mariadb": "mysql"}


def _lower(names: list[str] | None) -> set[str]:
    return {name.split(".")[-1].strip('"').lower() for name in names or []}


def table_is_visible(table_name: str, config: QueryPilotConfig) -> bool:
    """False for blocked tables and tables outside a non-empty ``allowed_tables``."""
    name = table_name.split(".")[-1].strip('"').lower()
    if config.blocked_tables and name in _lower(config.blocked_tables):
        return False
    return not (config.allowed_tables and name not in _lower(config.allowed_tables))


def column_is_visible(table_name: str, column_name: str, config: QueryPilotConfig) -> bool:
    """False for blocked columns and columns outside the table's ``allowed_columns``."""
    policy = config.access_policy
    table = table_name.split(".")[-1].strip('"').lower()
    column = column_name.lower()
    for blocked_table, columns in policy.blocked_columns.items():
        if blocked_table.lower() == table and column in {c.lower() for c in columns}:
            return False
    for allowed_table, columns in policy.allowed_columns.items():
        if allowed_table.lower() == table and column not in {c.lower() for c in columns}:
            return False
    return True


def sample_token_allowance(table: TableSchema, sample_values: int) -> int:
    """Rough prompt tokens the samples of ``table`` will add, before fetching them."""
    if sample_values <= 0:
        return 0
    text_columns = sum(
        1
        for column in table.columns
        if is_text_type(column.type) and not is_identifier_like(table, column)
    )
    return text_columns * (2 + 4 * sample_values)


def table_can_be_sampled(table_name: str, config: QueryPilotConfig) -> bool:
    if config.sample_values <= 0 or not table_is_visible(table_name, config):
        return False
    lowered = table_name.lower()
    return not any(name.lower() == lowered for name in config.access_policy.row_filters)


def column_can_be_sampled(table: TableSchema, column: ColumnSchema, config: QueryPilotConfig) -> bool:
    if not column_is_visible(table.name, column.name, config):
        return False
    for masked_table, rules in config.access_policy.masking_rules.items():
        if masked_table.lower() == table.name.lower() and column.name.lower() in {
            name.lower() for name in rules
        }:
            return False
    return is_text_type(column.type) and not is_identifier_like(table, column) and not (
        _SENSITIVE_NAME.search(column.name)
    )


def is_text_type(type_name: str) -> bool:
    upper = type_name.upper()
    return any(token in upper for token in _TEXT_TYPES) and not any(
        token in upper for token in _NON_TEXT_TYPES
    )


def is_identifier_like(table: TableSchema, column: ColumnSchema) -> bool:
    if column.primary_key:
        return True
    name = column.name
    lowered = name.lower()
    if lowered in {"id", "uuid", "guid", "key"} or lowered.endswith(("_id", "_uuid", "_guid", "_key")):
        return True
    if re.search(r"[a-z0-9](Id|ID|Uuid|UUID)$", name):
        return True
    fk_columns = {c.lower() for fk in table.foreign_keys for c in fk.columns}
    return lowered in fk_columns


def build_sample_query(table: TableSchema, column: ColumnSchema, dialect: str, limit: int) -> str:
    """Most frequent non-null values of one column, over a bounded scan of the table.

    ``SELECT col FROM (SELECT col FROM t LIMIT scan) WHERE col IS NOT NULL
    GROUP BY col ORDER BY COUNT(*) DESC, col LIMIT n`` -- the distinct values a
    question is most likely to mention, in a stable order.
    """
    sqlglot_dialect = _SQLGLOT_DIALECTS.get(dialect, dialect)
    table_expr = exp.to_table(
        f"{_quoted(table.schema_name)}.{_quoted(table.name)}"
        if table.schema_name
        else _quoted(table.name)
    )

    def column_expr() -> exp.Column:
        return exp.column(exp.to_identifier(column.name, quoted=True))

    scan = exp.select(column_expr()).from_(table_expr).limit(SCAN_ROWS)
    query = (
        exp.select(column_expr())
        .from_(scan.subquery("qp_sample"))
        .where(column_expr().is_(exp.null()).not_())
        .group_by(column_expr())
        .order_by(exp.Ordered(this=exp.Count(this=exp.Star()), desc=True), column_expr())
        .limit(limit)
    )
    return query.sql(dialect=sqlglot_dialect)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fetch_table_samples(
    connector: BaseConnector,
    table: TableSchema,
    dialect: str,
    config: QueryPilotConfig,
    columns: list[ColumnSchema] | None = None,
) -> dict[str, list[str]]:
    """Sample values keyed by lower-cased column name, for the sampleable columns only.

    ``columns`` limits fetching to a subset (still filtered by the policy); a
    column whose query fails or has no non-null values maps to ``[]``.
    """
    samples: dict[str, list[str]] = {}
    if not table_can_be_sampled(table.name, config):
        return samples
    for column in columns if columns is not None else table.columns:
        if not column_can_be_sampled(table, column, config):
            continue
        values: list[str] = []
        try:
            sql = build_sample_query(table, column, dialect, config.sample_values)
            rows, _ = connector.execute_readonly(sql)
        except Exception as exc:  # noqa: BLE001 - samples are best-effort
            logger.debug("Sampling %s.%s failed: %s", table.name, column.name, exc)
            rows = []
        for row in rows:
            value = next(iter(row.values()), None) if row else None
            text = _format_value(value)
            if text and text not in values:
                values.append(text)
        samples[column.name.lower()] = values[: config.sample_values]
    return samples


# Per-connector cache: (table, column) -> values. Keyed weakly on the connector so
# QueryPilot copies that share a connector (e.g. per-request audit scopes) share
# samples, and the cache goes away with the connector.
_CACHE: weakref.WeakKeyDictionary[BaseConnector, dict[tuple[str, str], list[str]]] = (
    weakref.WeakKeyDictionary()
)
_CACHE_LOCK = threading.Lock()


def cached_samples(
    connector: BaseConnector,
    table: TableSchema,
    dialect: str,
    config: QueryPilotConfig,
) -> dict[str, list[str]]:
    """Like ``fetch_table_samples`` but fetches each column at most once per connector.

    The policy is re-applied on every call, so a stricter config sharing the
    connector never sees values of a column it may not sample.
    """
    if not table_can_be_sampled(table.name, config):
        return {}
    wanted = [column for column in table.columns if column_can_be_sampled(table, column, config)]
    if not wanted:
        return {}
    table_key = table.name.lower()
    with _CACHE_LOCK:
        cache = _CACHE.setdefault(connector, {})
        missing = [column for column in wanted if (table_key, column.name.lower()) not in cache]
    if missing:
        fetched = fetch_table_samples(connector, table, dialect, config, columns=missing)
        with _CACHE_LOCK:
            for column in missing:
                cache[(table_key, column.name.lower())] = fetched.get(column.name.lower(), [])
    return {
        column.name.lower(): cache[(table_key, column.name.lower())][: config.sample_values]
        for column in wanted
        if cache.get((table_key, column.name.lower()))
    }


def clear_sample_cache(connector: BaseConnector | None = None) -> None:
    with _CACHE_LOCK:
        if connector is None:
            _CACHE.clear()
        else:
            _CACHE.pop(connector, None)


def apply_samples(table: TableSchema, samples: dict[str, list[str]]) -> TableSchema:
    """Copy of ``table`` with ``sample_values`` filled from ``samples``."""
    columns = [
        column.model_copy(update={"sample_values": list(samples.get(column.name.lower(), []))})
        for column in table.columns
    ]
    return table.model_copy(update={"columns": columns})


def _format_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > MAX_VALUE_CHARS:
        text = text[: MAX_VALUE_CHARS - 3].rstrip() + "..."
    return text
