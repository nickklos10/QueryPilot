from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from querypilot.evals import (
    BenchmarkSuite,
    ComparisonConfig,
    SuiteLoadError,
    SuiteThresholds,
    load_suite,
    load_suite_dir,
)
from querypilot.evals.suite import BenchmarkCase


def _write_yaml(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _minimal_suite_payload() -> dict:
    return {
        "name": "smoke",
        "fixture_db": "sqlite:///fixtures/demo.db",
        "cases": [
            {
                "id": "select_one",
                "question": "How many rows?",
                "gold_sql": "SELECT 1",
                "expected_tables": [],
                "tags": ["smoke"],
            }
        ],
    }


def test_loads_minimal_yaml_suite(tmp_path: Path) -> None:
    suite_path = _write_yaml(tmp_path / "smoke.yaml", _minimal_suite_payload())

    suite = load_suite(suite_path)

    assert isinstance(suite, BenchmarkSuite)
    assert suite.name == "smoke"
    assert suite.fixture_dialect == "sqlite"
    assert len(suite.cases) == 1
    case = suite.cases[0]
    assert isinstance(case, BenchmarkCase)
    assert case.id == "select_one"
    assert case.gold_sql == "SELECT 1"
    assert case.tags == ["smoke"]
    assert case.source == "authored"


def test_loads_minimal_json_suite(tmp_path: Path) -> None:
    suite_path = tmp_path / "smoke.json"
    suite_path.write_text(json.dumps(_minimal_suite_payload()), encoding="utf-8")

    suite = load_suite(suite_path)

    assert suite.name == "smoke"
    assert suite.cases[0].id == "select_one"


def test_resolves_relative_sqlite_fixture_path(tmp_path: Path) -> None:
    suites_dir = tmp_path / "suites"
    suites_dir.mkdir()
    payload = _minimal_suite_payload()
    payload["fixture_db"] = "sqlite:///../tests/fixtures/demo.db"
    suite_path = _write_yaml(suites_dir / "smoke.yaml", payload)

    suite = load_suite(suite_path)

    expected = (tmp_path / "tests" / "fixtures" / "demo.db").resolve()
    assert suite.fixture_db == f"sqlite:///{expected}"


def test_keeps_absolute_sqlite_fixture_path_unchanged(tmp_path: Path) -> None:
    payload = _minimal_suite_payload()
    payload["fixture_db"] = "sqlite:////absolute/path/to/demo.db"
    suite_path = _write_yaml(tmp_path / "smoke.yaml", payload)

    suite = load_suite(suite_path)

    assert suite.fixture_db == "sqlite:////absolute/path/to/demo.db"


def test_keeps_postgres_url_unchanged(tmp_path: Path) -> None:
    payload = _minimal_suite_payload()
    payload["fixture_db"] = "postgresql://user:pw@localhost:5432/db"
    suite_path = _write_yaml(tmp_path / "smoke.yaml", payload)

    suite = load_suite(suite_path)

    assert suite.fixture_db == "postgresql://user:pw@localhost:5432/db"


def test_keeps_in_memory_sqlite_unchanged(tmp_path: Path) -> None:
    payload = _minimal_suite_payload()
    payload["fixture_db"] = "sqlite:///:memory:"
    suite_path = _write_yaml(tmp_path / "smoke.yaml", payload)

    suite = load_suite(suite_path)

    assert suite.fixture_db == "sqlite:///:memory:"


def test_thresholds_and_comparison_config_parsed(tmp_path: Path) -> None:
    payload = _minimal_suite_payload()
    payload["thresholds"] = {
        "pass_rate": 0.95,
        "safety_pass_rate": 1.0,
        "correctness_rate": 0.9,
        "max_p95_latency_ms": 5000,
        "max_avg_cost_usd": 0.01,
    }
    payload["comparison"] = {
        "ignore_row_order": False,
        "ignore_column_order": False,
        "float_tolerance": 0.01,
        "normalize_datetimes": False,
        "case_insensitive_strings": True,
    }
    suite_path = _write_yaml(tmp_path / "smoke.yaml", payload)

    suite = load_suite(suite_path)

    assert suite.thresholds == SuiteThresholds(
        pass_rate=0.95,
        safety_pass_rate=1.0,
        correctness_rate=0.9,
        max_p95_latency_ms=5000,
        max_avg_cost_usd=0.01,
    )
    assert suite.comparison == ComparisonConfig(
        ignore_row_order=False,
        ignore_column_order=False,
        float_tolerance=0.01,
        normalize_datetimes=False,
        case_insensitive_strings=True,
    )


def test_default_thresholds_and_comparison(tmp_path: Path) -> None:
    suite_path = _write_yaml(tmp_path / "smoke.yaml", _minimal_suite_payload())

    suite = load_suite(suite_path)

    assert suite.thresholds == SuiteThresholds()
    assert suite.comparison == ComparisonConfig()


def test_expected_columns_and_must_include_parsed(tmp_path: Path) -> None:
    payload = _minimal_suite_payload()
    payload["cases"][0].update(
        {
            "expected_columns": ["customer_name", "revenue"],
            "must_include": ["ORDER BY", "LIMIT"],
            "must_not_contain": ["DELETE", "DROP"],
        }
    )
    suite_path = _write_yaml(tmp_path / "smoke.yaml", payload)

    suite = load_suite(suite_path)

    case = suite.cases[0]
    assert case.expected_columns == ["customer_name", "revenue"]
    assert case.must_include == ["ORDER BY", "LIMIT"]
    assert case.must_not_contain == ["DELETE", "DROP"]


def test_safety_case_with_sql_only(tmp_path: Path) -> None:
    payload = {
        "name": "safety",
        "fixture_db": "sqlite:///x.db",
        "cases": [
            {
                "id": "blocks_drop",
                "sql": "DROP TABLE customers",
                "should_pass": False,
                "expected_failure_kind": "validation",
                "expected_error_contains": ["Only SELECT"],
                "tags": ["safety"],
            }
        ],
    }
    suite_path = _write_yaml(tmp_path / "safety.yaml", payload)

    suite = load_suite(suite_path)

    case = suite.cases[0]
    assert case.should_pass is False
    assert case.expected_failure_kind == "validation"
    assert case.sql == "DROP TABLE customers"
    assert case.question is None


def test_negative_case_requires_expected_failure_kind(tmp_path: Path) -> None:
    payload = {
        "name": "safety",
        "fixture_db": "sqlite:///x.db",
        "cases": [
            {
                "id": "missing_kind",
                "sql": "DROP TABLE customers",
                "should_pass": False,
            }
        ],
    }
    suite_path = _write_yaml(tmp_path / "safety.yaml", payload)

    with pytest.raises(SuiteLoadError, match="expected_failure_kind"):
        load_suite(suite_path)


def test_case_requires_question_or_sql(tmp_path: Path) -> None:
    payload = {
        "name": "smoke",
        "fixture_db": "sqlite:///x.db",
        "cases": [{"id": "empty"}],
    }
    suite_path = _write_yaml(tmp_path / "smoke.yaml", payload)

    with pytest.raises(SuiteLoadError, match="question"):
        load_suite(suite_path)


def test_duplicate_case_id_rejected(tmp_path: Path) -> None:
    payload = _minimal_suite_payload()
    payload["cases"].append(dict(payload["cases"][0]))
    suite_path = _write_yaml(tmp_path / "smoke.yaml", payload)

    with pytest.raises(SuiteLoadError, match="Duplicate case id"):
        load_suite(suite_path)


def test_missing_fixture_db_on_case_and_suite_rejected(tmp_path: Path) -> None:
    payload = {
        "name": "smoke",
        "cases": [
            {"id": "x", "question": "?", "gold_sql": "SELECT 1"},
        ],
    }
    suite_path = _write_yaml(tmp_path / "smoke.yaml", payload)

    with pytest.raises(SuiteLoadError, match="fixture_db"):
        load_suite(suite_path)


def test_case_level_fixture_db_overrides_suite(tmp_path: Path) -> None:
    payload = {
        "name": "mixed",
        "fixture_db": "sqlite:///suite-default.db",
        "cases": [
            {
                "id": "uses_suite_default",
                "question": "?",
                "gold_sql": "SELECT 1",
            },
            {
                "id": "uses_case_override",
                "question": "?",
                "gold_sql": "SELECT 1",
                "fixture_db": "sqlite:///case-override.db",
            },
        ],
    }
    suite_path = _write_yaml(tmp_path / "mixed.yaml", payload)

    suite = load_suite(suite_path)

    assert suite.resolved_fixture_db(suite.cases[0]).endswith("suite-default.db")
    assert suite.resolved_fixture_db(suite.cases[1]).endswith("case-override.db")


def test_invalid_yaml_raises_load_error(tmp_path: Path) -> None:
    suite_path = tmp_path / "broken.yaml"
    suite_path.write_text("name: x\n  bad: : :", encoding="utf-8")

    with pytest.raises(SuiteLoadError, match="Invalid YAML"):
        load_suite(suite_path)


def test_invalid_json_raises_load_error(tmp_path: Path) -> None:
    suite_path = tmp_path / "broken.json"
    suite_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SuiteLoadError, match="Invalid JSON"):
        load_suite(suite_path)


def test_unsupported_extension_raises(tmp_path: Path) -> None:
    suite_path = tmp_path / "smoke.txt"
    suite_path.write_text("x", encoding="utf-8")

    with pytest.raises(SuiteLoadError, match="Unsupported"):
        load_suite(suite_path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SuiteLoadError, match="not found"):
        load_suite(tmp_path / "nope.yaml")


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    suite_path = tmp_path / "list.yaml"
    suite_path.write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(SuiteLoadError, match="mapping"):
        load_suite(suite_path)


def test_load_suite_dir_merges_files(tmp_path: Path) -> None:
    smoke = tmp_path / "smoke.yaml"
    safety = tmp_path / "safety.yaml"
    _write_yaml(smoke, _minimal_suite_payload())
    _write_yaml(
        safety,
        {
            "name": "safety",
            "fixture_db": "sqlite:///x.db",
            "cases": [
                {
                    "id": "blocks_drop",
                    "sql": "DROP TABLE x",
                    "should_pass": False,
                    "expected_failure_kind": "validation",
                }
            ],
        },
    )

    merged = load_suite_dir(tmp_path)

    case_ids = {c.id for c in merged.cases}
    assert case_ids == {"select_one", "blocks_drop"}


def test_load_suite_dir_rejects_duplicate_ids_across_files(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    _write_yaml(a, _minimal_suite_payload())
    _write_yaml(b, _minimal_suite_payload())

    with pytest.raises(SuiteLoadError, match="Duplicate"):
        load_suite_dir(tmp_path)


def test_load_suite_dir_requires_some_files(tmp_path: Path) -> None:
    with pytest.raises(SuiteLoadError, match="No .yaml/.yml/.json"):
        load_suite_dir(tmp_path)


def test_audit_replay_metadata_preserved(tmp_path: Path) -> None:
    payload = {
        "name": "replay",
        "fixture_db": "sqlite:///x.db",
        "cases": [
            {
                "id": "audit-7c2a1f9b",
                "source": "audit_replay",
                "audit_id": "7c2a1f9b-...",
                "original_timestamp": "2026-04-26T18:32:11Z",
                "original_question": "Top customers?",
                "question": "Top customers?",
                "gold_sql": "SELECT * FROM customers LIMIT 10",
                "tags": ["replay", "regression", "app:billing-bot"],
            }
        ],
    }
    suite_path = _write_yaml(tmp_path / "replay.yaml", payload)

    suite = load_suite(suite_path)

    case = suite.cases[0]
    assert case.source == "audit_replay"
    assert case.audit_id == "7c2a1f9b-..."
    assert case.original_timestamp == "2026-04-26T18:32:11Z"
    assert case.original_question == "Top customers?"


def test_bundled_smoke_suite_loads() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    suite = load_suite(repo_root / "suites" / "smoke.yaml")

    assert suite.name == "smoke"
    assert suite.fixture_db is not None and suite.fixture_db.startswith("sqlite:///")
    assert {c.id for c in suite.cases} == {
        "top_customers_by_revenue",
        "customer_count",
        "monthly_invoice_revenue",
    }


def test_bundled_safety_suite_loads() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    suite = load_suite(repo_root / "suites" / "safety.yaml")

    assert suite.name == "safety"
    assert all(not c.should_pass for c in suite.cases)
    assert all(c.expected_failure_kind == "validation" for c in suite.cases)
