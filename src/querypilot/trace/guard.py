"""Check an agent's SQL before the query tool runs it.

The guard validates column and table references with ``SQLValidator`` against
the schema cache and returns "did you mean" feedback, so the agent fixes a bad
column without a round trip to the warehouse. It only judges queries whose
datasets are all cached; anything it cannot parse or resolve is allowed
(fail open), because a guard that blocks valid SQL costs more than it saves.
It can also require a row cap (a LIMIT, or the tool's own row-limit input).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from querypilot.core.config import QueryPilotConfig, SafetyPolicy
from querypilot.core.types import ColumnSchema, DatabaseSchema, TableSchema
from querypilot.trace.classify import GUARD_PREFIX
from querypilot.trace.schema_cache import (
    FOUNDRY_RID,
    CachedDataset,
    SchemaCache,
    table_ref_name,
    table_refs,
)
from querypilot.validation.feedback import repair_hints
from querypilot.validation.validator import SQLValidator

ALLOW = "allow"
DENY = "deny"

_RESOLUTION_ERRORS = ("Unknown column:", "Ambiguous column:", "Unknown table qualifier:")


@dataclass
class GuardDecision:
    action: str = ALLOW
    reason: str | None = None
    # "schema" (unknown column/table), "limit" (no row cap), "schema_required".
    rule: str | None = None
    # SQL with a LIMIT added, for callers that rewrite instead of denying.
    limited_sql: str | None = None
    # True when every referenced dataset was in the cache, so columns were checked.
    checked_columns: bool = False
    tables: list[str] = field(default_factory=list)

    @property
    def denied(self) -> bool:
        return self.action == DENY


def guard_sql(
    sql: str,
    cache: SchemaCache,
    *,
    dialect: str = "spark",
    max_rows: int | None = None,
    tool_row_limit: int | None = None,
    require_schema: bool = False,
    schema_tool_hint: str = "the schema tool",
) -> GuardDecision:
    stripped = sql.strip().rstrip(";").strip()
    try:
        expression = sqlglot.parse_one(stripped, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        # The engine's parser is the authority on syntax; ours may lag its dialect.
        return GuardDecision()
    if expression is None:
        return GuardDecision()

    refs = table_refs(expression)
    names = list(dict.fromkeys(table_ref_name(ref) for ref in refs))
    decision = GuardDecision(tables=names)

    # Transaction-pinned refs (ri.foundry.main.transaction...@branch.ri...) are valid; only
    # check names that claim to be a bare dataset RID.
    malformed = [
        name for name in names if name.startswith("ri.foundry.main.dataset.") and not FOUNDRY_RID.fullmatch(name)
    ]
    if malformed:
        decision.action, decision.rule = DENY, "schema"
        decision.reason = _message(
            [f"`{name}` is not a valid dataset RID (expected ri.foundry.main.dataset.<uuid>)." for name in malformed]
            + _close_rids(malformed, cache)
        )
        return decision

    datasets = {name: cache.lookup(name) for name in names}
    missing = [name for name, dataset in datasets.items() if dataset is None]
    if missing and require_schema:
        decision.action, decision.rule = DENY, "schema_required"
        decision.reason = _message(
            [
                ("Fetch the schema before querying: call "
                f"{schema_tool_hint} for {', '.join(f'`{name}`' for name in missing)}, "
                "then write the query with the real column names.")
            ]
        )
        return decision

    if names and not missing:
        decision.checked_columns = True
        problems = _column_problems(expression, refs, datasets, dialect) or _epoch_misuse(expression, datasets)
        if problems:
            decision.action, decision.rule = DENY, "schema"
            decision.reason = _message(problems)
            return decision

    if max_rows is not None and not _row_capped(expression, max_rows, tool_row_limit):
        decision.limited_sql = _with_limit(stripped, expression, max_rows, dialect)
        decision.action, decision.rule = DENY, "limit"
        decision.reason = _message(
            [
                (f"Queries must return at most {max_rows} rows: add `LIMIT {max_rows}` "
                "(or a smaller one), or aggregate. Large results overflow the context window.")
            ]
        )
    return decision


def _column_problems(
    expression: exp.Expression,
    refs: list[exp.Table],
    datasets: dict[str, CachedDataset | None],
    dialect: str,
) -> list[str]:
    aliases = _aliases(datasets)
    renamed = expression.copy()
    for table in table_refs(renamed):
        alias = aliases[table_ref_name(table)]
        table.set("this", exp.to_identifier(alias))
        table.set("db", None)
        table.set("catalog", None)
    schema = DatabaseSchema(
        dialect=dialect,
        tables=[
            TableSchema(
                name=aliases[name],
                columns=[ColumnSchema(name=c.name, type=c.type, comment=c.description or None) for c in ds.columns],
            )
            for name, ds in datasets.items()
            if ds is not None
        ],
    )
    config = QueryPilotConfig(
        dialect=dialect,
        max_rows=1_000_000,
        safety_policy=SafetyPolicy(reject_cartesian_joins=False, blocked_functions=[], warn_on_select_star=False),
    )
    renamed_sql = renamed.sql(dialect=dialect)
    try:
        result = SQLValidator(config).validate(renamed_sql, schema)
    except Exception:  # noqa: BLE001 - fail open on validator crashes
        return []
    # Spark resolves `struct_col.field` and backticked names containing dots; the
    # validator reads both as table.column, so those references are not judged.
    value_aliases = {alias.alias.lower() for alias in expression.find_all(exp.Alias) if alias.alias}
    dotted = any("." in ident.name for col in expression.find_all(exp.Column) for ident in col.parts)
    if dotted:
        return []
    errors = [
        error
        for error in result.errors
        if error.startswith(_RESOLUTION_ERRORS)
        and not (
            error.startswith("Unknown table qualifier:")
            and error.split(":", 1)[1].strip().lower() in value_aliases | _column_names(datasets)
        )
    ]
    if not errors:
        return []
    hints = repair_hints(errors, schema, renamed_sql) or errors
    legend = [
        f"{aliases[name]} = `{name}`" + (f" ({ds.path})" if ds and ds.path and ds.path != name else "")
        for name, ds in datasets.items()
        if aliases[name] != name
    ]
    if legend:
        hints.append("Dataset names above: " + "; ".join(legend) + ".")
    return hints


def _column_names(datasets: dict[str, CachedDataset | None]) -> set[str]:
    return {column.name.lower() for dataset in datasets.values() if dataset for column in dataset.columns}


_DATE_TYPES = ("DATE", "TIMESTAMP")
_ARITHMETIC_OR_COMPARISON = (exp.Div, exp.Mul, exp.Add, exp.Sub, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)


def _epoch_misuse(expression: exp.Expression, datasets: dict[str, CachedDataset | None]) -> list[str]:
    """DATE/TIMESTAMP columns used as epoch milliseconds (``Report_Date / 1000``)."""
    types: dict[str, str] = {}
    for dataset in datasets.values():
        for column in dataset.columns if dataset else []:
            types.setdefault(column.name.lower(), column.type.upper())
    problems: list[str] = []
    for node in expression.find_all(*_ARITHMETIC_OR_COMPARISON):
        left, right = node.left, node.right
        for column, other in ((left, right), (right, left)):
            if not isinstance(column, exp.Column):
                continue
            column_type = types.get(column.name.lower(), "")
            if not column_type.startswith(_DATE_TYPES) or not _epoch_like(other):
                continue
            message = (
                f"`{column.name}` is a {column_type} column, not epoch milliseconds: "
                f"`{node.sql()}` fails with a type mismatch. Compare it with a literal like "
                f"DATE '2026-01-01' or use date functions (year(), date_trunc(), datediff())."
            )
            if message not in problems:
                problems.append(message)
    return problems


def _epoch_like(node: exp.Expression) -> bool:
    numbers = [node] if isinstance(node, exp.Literal) else list(node.find_all(exp.Literal))
    for literal in numbers:
        if not literal.is_number:
            continue
        try:
            value = abs(float(literal.this))
        except ValueError:
            continue
        # 1000 (ms <-> s), 86400000 (ms per day), or a 13-digit epoch-ms timestamp.
        if value in (1000, 86_400_000) or value >= 1e11:
            return True
    return False


def _aliases(datasets: dict[str, CachedDataset | None]) -> dict[str, str]:
    out: dict[str, str] = {}
    used: set[str] = set()
    for name, dataset in datasets.items():
        base = dataset.display_name if dataset else name
        alias = re.sub(r"\W+", "_", base).strip("_") or "dataset"
        if alias[0].isdigit():
            alias = f"d_{alias}"
        candidate, n = alias, 2
        while candidate.lower() in used:
            candidate, n = f"{alias}_{n}", n + 1
        used.add(candidate.lower())
        out[name] = candidate
    return out


def _close_rids(names: list[str], cache: SchemaCache) -> list[str]:
    import difflib

    known = [key for key in cache.datasets if key.startswith("ri.")]
    out = []
    for name in names:
        close = difflib.get_close_matches(name, known, n=2, cutoff=0.8)
        if close:
            out.append(f"Did you mean {', '.join(f'`{c}`' for c in close)}?")
    return out


def _row_capped(expression: exp.Expression, max_rows: int, tool_row_limit: int | None) -> bool:
    if tool_row_limit is not None and 0 < tool_row_limit <= max_rows:
        return True
    limit = expression.args.get("limit")
    value = limit.args.get("expression") if isinstance(limit, exp.Limit) else None
    if isinstance(value, exp.Literal) and value.is_number:
        try:
            return 0 <= int(value.this) <= max_rows
        except ValueError:
            return False
    # An aggregate without GROUP BY returns one row.
    if isinstance(expression, exp.Select) and not expression.args.get("group"):
        projections = expression.expressions
        return bool(projections) and all(p.find(exp.AggFunc) is not None for p in projections)
    return False


def _with_limit(stripped: str, expression: exp.Expression, max_rows: int, dialect: str) -> str:
    if expression.args.get("limit") is None and "--" not in stripped:
        return f"{stripped}\nLIMIT {max_rows}"
    capped = expression.copy()
    capped.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return capped.sql(dialect=dialect)


def _message(lines: list[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines)
    return f"{GUARD_PREFIX} blocked this query before it ran:\n{body}\nFix the SQL and call the tool again."
