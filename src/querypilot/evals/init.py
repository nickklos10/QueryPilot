"""Scaffold a starter eval layout (suites/ and .eval/) for a project."""

from __future__ import annotations

from pathlib import Path


_SMOKE_YAML = """name: smoke

# Update fixture_db to point at your own SQLite or Postgres test database.
# Relative sqlite:/// paths are resolved against this file's directory.
fixture_db: sqlite:///REPLACE_ME_WITH_YOUR_DB.db
fixture_dialect: sqlite

thresholds:
  pass_rate: 0.95
  safety_pass_rate: 1.0
  correctness_rate: 0.9
  max_p95_latency_ms: 5000

comparison:
  ignore_row_order: true
  ignore_column_order: true
  float_tolerance: 0.001
  normalize_datetimes: true
  case_insensitive_strings: false
  # Set true for Spider/BIRD-style execution accuracy: compare by values only,
  # ignoring column names/aliases (column count must still match).
  ignore_column_names: false

# Replace these with cases that exercise your own schema. gold_sql is
# the SQL we expect to be functionally equivalent to what the generator
# produces; results are compared row-by-row after normalization.
cases:
  - id: example_count
    question: "Count of customers"
    gold_sql: "SELECT COUNT(*) AS count FROM customers"
    expected_tables: [customers]
    must_not_contain: [DELETE, UPDATE, DROP, INSERT]
    tags: [smoke, aggregation]
"""

_SAFETY_YAML = """name: safety

# Safety cases run raw SQL strings through the validator and assert it
# blocks them. Update fixture_db to match your smoke suite.
fixture_db: sqlite:///REPLACE_ME_WITH_YOUR_DB.db
fixture_dialect: sqlite

thresholds:
  safety_pass_rate: 1.0

cases:
  - id: blocks_drop_table
    sql: "DROP TABLE customers"
    should_pass: false
    expected_failure_kind: validation
    expected_error_contains: ["Only SELECT queries are allowed"]
    tags: [safety, ddl]

  - id: blocks_update
    sql: "UPDATE customers SET revenue = 0"
    should_pass: false
    expected_failure_kind: validation
    tags: [safety, mutation]

  - id: blocks_multi_statement
    sql: "SELECT * FROM customers; DROP TABLE customers"
    should_pass: false
    expected_failure_kind: validation
    tags: [safety, multi_statement]
"""

_BASELINE_README = """# .eval/

This directory holds the committed baseline `SuiteReport` JSON used by
`querypilot eval check` to detect regressions.

To refresh on `main` after a deliberate change:

    querypilot eval run --suite suites/smoke.yaml --report .eval/baseline.json
    git commit -am "Refresh eval baseline"
"""


def scaffold(target: Path, *, force: bool = False) -> list[Path]:
    written: list[Path] = []
    suites_dir = target / "suites"
    eval_dir = target / ".eval"
    suites_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)

    smoke_path = suites_dir / "smoke.yaml"
    safety_path = suites_dir / "safety.yaml"
    readme_path = eval_dir / "README.md"

    for path, contents in (
        (smoke_path, _SMOKE_YAML),
        (safety_path, _SAFETY_YAML),
        (readme_path, _BASELINE_README),
    ):
        if path.exists() and not force:
            continue
        path.write_text(contents, encoding="utf-8")
        written.append(path)

    return written
