"""Offline schema-context quality report for the SaaSPulse benchmark (no LLM).

For every correctness case in ``saaspulse/correctness.yaml`` it compares the
prompt QueryPilot sends to an LLM before and after the schema-context work:

* prompt size (estimated tokens, four characters per token) of the legacy
  ``table(col type, ...)`` prompt, the new full-context prompt, and the new
  prompt under small token budgets (``--budget``, default 1200 600 400). The
  SaaSPulse schema fits in 1200, so pruning is a no-op there by design; the
  smaller budgets force it to run;
* recall of the pruned context: whether every table and column the gold SQL
  reads is still in the prompt (the cost of pruning; 100% means no loss);
* join coverage: gold joins whose columns are linked by a foreign-key line;
* literal coverage: string literals the gold SQL filters on that appear among
  the sample values shown in the prompt (the legacy prompt shows none).

Run from the repo root:

    python benchmarks/schema_context_report.py [--budget 1200] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import sqlglot
from sqlglot import exp

from querypilot import QueryPilot
from querypilot.core.types import DatabaseSchema
from querypilot.evals.loader import load_suite
from querypilot.generation.prompt_builder import build_sql_generation_prompt
from querypilot.schema.pruning import estimate_tokens

ROOT = Path(__file__).resolve().parent
SUITE = ROOT / "saaspulse" / "correctness.yaml"
FIXTURE = ROOT / "fixtures" / "saaspulse.db"
MAX_ROWS = 100


def legacy_prompt(question: str, schema: DatabaseSchema, max_rows: int) -> str:
    """The prompt as rendered before this change (for the token baseline)."""
    instructions = (
        "You generate safe SQL for QueryPilot. Return only JSON with keys "
        "`sql` and `explanation`. Generate SELECT-only SQL. Use only tables "
        "and columns present in the provided schema. Do not use mutations, DDL, "
        "multiple statements, comments, or vendor-specific unsafe commands. "
        f"The maximum row limit is {max_rows}; include a LIMIT no larger than that "
        "unless the query is an aggregate that returns one row."
    )
    lines = [
        f"{t.name}({', '.join(f'{c.name} {c.type}' for c in t.columns)})" for t in schema.tables
    ]
    return instructions + "\n" + "\n".join([f"Question: {question}", "", "Schema:", *lines])


def gold_references(sql: str, schema: DatabaseSchema) -> tuple[set[str], set[tuple[str, str]]]:
    """Base tables and (table, column) pairs the gold SQL reads."""
    tree = sqlglot.parse_one(sql, read="sqlite")
    names = schema.table_names
    aliases: dict[str, str] = {}
    tables: set[str] = set()
    for node in tree.find_all(exp.Table):
        name = node.name.lower()
        if name in names:
            tables.add(name)
            aliases[name] = name
            if node.alias:
                aliases[node.alias.lower()] = name
    columns: set[tuple[str, str]] = set()
    for node in tree.find_all(exp.Column):
        column = node.name.lower()
        if node.table and node.table.lower() in aliases:
            owners = [aliases[node.table.lower()]]
        else:
            owners = [t for t in tables if (tbl := schema.get_table(t)) and tbl.get_column(column)]
        for owner in owners[:1]:
            columns.add((owner, column))
    return tables, columns


def gold_joins(sql: str, schema: DatabaseSchema) -> list[frozenset[tuple[str, str]]]:
    tree = sqlglot.parse_one(sql, read="sqlite")
    aliases = {}
    for node in tree.find_all(exp.Table):
        if node.name.lower() in schema.table_names:
            aliases[node.name.lower()] = node.name.lower()
            if node.alias:
                aliases[node.alias.lower()] = node.name.lower()
    joins = []
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            left, right = eq.this, eq.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                a = (aliases.get(left.table.lower(), ""), left.name.lower())
                b = (aliases.get(right.table.lower(), ""), right.name.lower())
                if a[0] and b[0] and a[0] != b[0]:
                    joins.append(frozenset({a, b}))
    return joins


def fk_pairs(schema: DatabaseSchema) -> set[frozenset[tuple[str, str]]]:
    pairs = set()
    for table in schema.tables:
        for fk in table.foreign_keys:
            for col, ref in zip(fk.columns, fk.referred_columns, strict=False):
                pairs.add(
                    frozenset({(table.name.lower(), col.lower()), (fk.referred_table.lower(), ref.lower())})
                )
    return pairs


def gold_literals(sql: str) -> list[tuple[str, str]]:
    """(column, literal) for ``col = 'x'`` / ``col IN ('x', ...)`` filters."""
    tree = sqlglot.parse_one(sql, read="sqlite")
    found = []
    for node in tree.find_all(exp.EQ, exp.NEQ, exp.In):
        column = node.this if isinstance(node.this, exp.Column) else None
        if column is None:
            continue
        values = node.expressions if isinstance(node, exp.In) else [node.expression]
        for value in values:
            if isinstance(value, exp.Literal) and value.is_string:
                found.append((column.name.lower(), value.this))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=int, nargs="+", default=[1200, 600, 400])
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    suite = load_suite(SUITE)
    cases = [case for case in suite.cases if case.gold_sql and case.question]
    url = f"sqlite:///{FIXTURE}"
    full_qp = QueryPilot.connect(url)
    raw = full_qp.get_schema()
    fks = fk_pairs(raw)

    baseline = []
    for case in cases:
        full = full_qp.schema_for_prompt(case.question)
        prompt = build_sql_generation_prompt(case.question, full, MAX_ROWS)
        joins = gold_joins(case.gold_sql, raw)
        literals = gold_literals(case.gold_sql)
        sampled = {(c.name.lower(), v) for t in full.tables for c in t.columns for v in c.sample_values}
        baseline.append(
            {
                "legacy_tokens": estimate_tokens(legacy_prompt(case.question, raw, MAX_ROWS)),
                "full_tokens": estimate_tokens(prompt.instructions + "\n" + prompt.user_prompt),
                "gold_joins": len(joins),
                "gold_joins_fk": sum(1 for j in joins if j in fks),
                "literals": len(literals),
                "literals_sampled": sum(1 for lit in literals if lit in sampled),
            }
        )

    def total(rows: list[dict], key: str) -> int:
        return sum(row[key] for row in rows)

    report: dict = {
        "cases": len(cases),
        "avg_prompt_tokens_legacy": round(statistics.fmean(r["legacy_tokens"] for r in baseline)),
        "avg_prompt_tokens_full_context": round(statistics.fmean(r["full_tokens"] for r in baseline)),
        "gold_joins_covered_by_fk_lines": (
            f"{total(baseline, 'gold_joins_fk')}/{total(baseline, 'gold_joins')}"
        ),
        "gold_string_literals_shown_as_samples": (
            f"{total(baseline, 'literals_sampled')}/{total(baseline, 'literals')}"
        ),
        "budgets": {},
    }

    per_case: dict[str, dict] = {}
    for budget in args.budget:
        qp = QueryPilot.connect(url, schema_token_budget=budget)
        rows = []
        for case in cases:
            pruned = qp.schema_for_prompt(case.question)
            prompt = build_sql_generation_prompt(case.question, pruned, MAX_ROWS)
            tables, columns = gold_references(case.gold_sql, raw)
            kept = pruned.table_names
            kept_columns = {(t.name.lower(), c.name.lower()) for t in pruned.tables for c in t.columns}
            row = {
                "tokens": estimate_tokens(prompt.instructions + "\n" + prompt.user_prompt),
                "tables_kept": len(pruned.tables),
                "gold_tables": len(tables),
                "gold_tables_kept": len(tables & kept),
                "gold_columns": len(columns),
                "gold_columns_kept": len(columns & kept_columns),
                "missing_tables": sorted(tables - kept),
            }
            rows.append(row)
            per_case.setdefault(case.id, {})[str(budget)] = row
        report["budgets"][str(budget)] = {
            "avg_prompt_tokens": round(statistics.fmean(r["tokens"] for r in rows)),
            "cases_pruned": sum(1 for r in rows if r["tables_kept"] < len(raw.tables)),
            "avg_tables_kept": round(statistics.fmean(r["tables_kept"] for r in rows), 2),
            "table_recall": round(total(rows, "gold_tables_kept") / max(total(rows, "gold_tables"), 1), 4),
            "column_recall": round(
                total(rows, "gold_columns_kept") / max(total(rows, "gold_columns"), 1), 4
            ),
            "cases_with_full_recall": sum(
                1
                for r in rows
                if r["gold_tables_kept"] == r["gold_tables"]
                and r["gold_columns_kept"] == r["gold_columns"]
            ),
            "cases_missing_tables": {
                case.id: r["missing_tables"] for case, r in zip(cases, rows, strict=True) if r["missing_tables"]
            },
        }

    print(json.dumps(report, indent=2))
    if args.json:
        args.json.write_text(json.dumps({"summary": report, "cases": per_case}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
