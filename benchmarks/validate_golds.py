"""Pre-flight validator for the SaaSPulse benchmark suite.

Runs two independent checks and exits non-zero if either finds a problem:

1. Correctness cases (those with ``gold_sql``): every gold query is executed
   against the fixture. A gold that raises, or that returns zero rows when it
   should return data, is a hard failure. This is what keeps the harness free of
   ``unknown_error`` categories caused by broken gold SQL.
2. Safety cases (``should_pass: false``): every one is pushed through the real
   QueryPilot validator, exactly as the eval harness does, and must be BLOCKED,
   for the ``validation`` reason, with every ``expected_error_contains`` string
   present in the validator output.

Run from the repo root (fixture must exist first):

    python benchmarks/fixtures/make_saaspulse.py
    python benchmarks/validate_golds.py

Optionally allow specific gold ids to return zero rows:

    python benchmarks/validate_golds.py --allow-empty some_case_id
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from querypilot import QueryPilot
from querypilot.evals.loader import load_suite_dir
from querypilot.evals.suite import BenchmarkCase, BenchmarkSuite
from querypilot.generation.sql_generator import DemoSQLGenerator

_SQLITE_PREFIX = "sqlite:///"
DEFAULT_SUITE_DIR = Path(__file__).resolve().parent / "saaspulse"


def _sqlite_path(fixture_db: str) -> Path:
    if not fixture_db.startswith(_SQLITE_PREFIX):
        raise SystemExit(f"Expected a sqlite:/// fixture URL, got {fixture_db!r}.")
    return Path(fixture_db[len(_SQLITE_PREFIX) :])


def _check_gold(
    conn: sqlite3.Connection, case: BenchmarkCase, allow_empty: set[str]
) -> str | None:
    assert case.gold_sql is not None
    try:
        rows = conn.execute(case.gold_sql).fetchall()
    except sqlite3.Error as exc:
        return f"gold SQL raised: {type(exc).__name__}: {exc}"
    if not rows and case.id not in allow_empty:
        return "gold SQL returned zero rows (pass --allow-empty if intentional)"
    if len(rows) > 100:
        # The validator caps candidates at max_rows=100; a gold above that can
        # never be matched by a candidate, so it is almost certainly a mistake.
        return f"gold SQL returned {len(rows)} rows (> 100); add an explicit LIMIT"
    return None


def _matches_expected(error_text: str, expected: list[str]) -> bool:
    lowered = error_text.lower()
    return all(needle.lower() in lowered for needle in expected)


def _check_safety(qp: QueryPilot, case: BenchmarkCase) -> str | None:
    if case.sql is None:
        return "safety case has no `sql` to validate"
    result = qp.validate_sql(case.sql)
    if result.valid:
        return "validator ALLOWED unsafe SQL (expected it to be blocked)"
    if (case.expected_failure_kind or "validation") != "validation":
        return (
            f"safety cases must use expected_failure_kind: validation, got "
            f"{case.expected_failure_kind!r}"
        )
    error_text = "; ".join(result.errors)
    if not _matches_expected(error_text, case.expected_error_contains):
        return (
            f"validator blocked, but errors {result.errors!r} do not contain all "
            f"of {case.expected_error_contains!r}"
        )
    return None


def validate(suite: BenchmarkSuite, allow_empty: set[str]) -> list[str]:
    failures: list[str] = []
    gold_cases = [c for c in suite.cases if c.gold_sql is not None]
    safety_cases = [c for c in suite.cases if not c.should_pass]

    fixture_path = _sqlite_path(suite.resolved_fixture_db(gold_cases[0]))
    if not fixture_path.is_file():
        raise SystemExit(
            f"Fixture not found: {fixture_path}. Run "
            "`python benchmarks/fixtures/make_saaspulse.py` first."
        )

    conn = sqlite3.connect(fixture_path)
    try:
        for case in gold_cases:
            problem = _check_gold(conn, case, allow_empty)
            if problem:
                failures.append(f"[gold] {case.id}: {problem}")
    finally:
        conn.close()

    qp = QueryPilot.connect(
        database_url=suite.resolved_fixture_db(safety_cases[0]),
        dialect="sqlite",
        generator=DemoSQLGenerator(),
    )
    for case in safety_cases:
        problem = _check_safety(qp, case)
        if problem:
            failures.append(f"[safety] {case.id}: {problem}")

    print(
        f"Checked {len(gold_cases)} gold queries and {len(safety_cases)} safety "
        f"cases ({len(suite.cases)} total) against {fixture_path.name}."
    )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate SaaSPulse gold + safety cases.")
    parser.add_argument(
        "--suite",
        type=Path,
        default=DEFAULT_SUITE_DIR,
        help="Suite directory (default: benchmarks/saaspulse).",
    )
    parser.add_argument(
        "--allow-empty",
        nargs="*",
        default=[],
        metavar="CASE_ID",
        help="Gold case ids permitted to return zero rows.",
    )
    args = parser.parse_args()

    suite = load_suite_dir(args.suite)
    failures = validate(suite, set(args.allow_empty))

    if failures:
        print(f"\nFAILED: {len(failures)} problem(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("OK: all gold queries executed with data and all safety cases blocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
