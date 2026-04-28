from __future__ import annotations

from pathlib import Path

import pytest

from querypilot import QueryPilot
from querypilot.core.types import GeneratedSQL
from querypilot.evals import (
    BenchmarkCase,
    BenchmarkSuite,
    ComparisonConfig,
    SuiteReport,
    SuiteThresholds,
    TagRollup,
    run_suite,
)


FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "demo.db"


@pytest.fixture(scope="module")
def fixture_db_url() -> str:
    if not FIXTURE_DB.exists():
        from tests.fixtures.seed_demo import seed

        seed(FIXTURE_DB)
    return f"sqlite:///{FIXTURE_DB}"


def _factory(generator=None):
    def _make(case: BenchmarkCase) -> QueryPilot:
        url = case.fixture_db or f"sqlite:///{FIXTURE_DB}"
        return QueryPilot.connect(database_url=url, dialect="sqlite", generator=generator)

    return _make


def _suite(
    *cases: BenchmarkCase,
    name: str = "test_suite",
    thresholds: SuiteThresholds | None = None,
    comparison: ComparisonConfig | None = None,
) -> BenchmarkSuite:
    return BenchmarkSuite(
        name=name,
        fixture_db=f"sqlite:///{FIXTURE_DB}",
        thresholds=thresholds or SuiteThresholds(),
        comparison=comparison or ComparisonConfig(),
        cases=list(cases),
    )


def _nl_case(case_id: str, **overrides) -> BenchmarkCase:
    base = {
        "id": case_id,
        "question": "Count of customers",
        "gold_sql": "SELECT COUNT(*) AS count FROM customers",
        "expected_tables": ["customers"],
        "fixture_db": f"sqlite:///{FIXTURE_DB}",
        "tags": ["smoke"],
    }
    base.update(overrides)
    return BenchmarkCase(**base)


def _safety_case(case_id: str, **overrides) -> BenchmarkCase:
    base = {
        "id": case_id,
        "sql": "DROP TABLE customers",
        "should_pass": False,
        "expected_failure_kind": "validation",
        "fixture_db": f"sqlite:///{FIXTURE_DB}",
        "tags": ["safety"],
    }
    base.update(overrides)
    return BenchmarkCase(**base)


def test_run_suite_returns_report_with_basic_aggregates(fixture_db_url: str) -> None:
    suite = _suite(_nl_case("a"), _nl_case("b"))

    report = run_suite(suite, qp_factory=_factory())

    assert isinstance(report, SuiteReport)
    assert report.suite_name == "test_suite"
    assert report.total_cases == 2
    assert report.passed == 2
    assert report.failed == 0
    assert report.pass_rate == 1.0
    assert report.threshold_violations == []
    assert report.duration_ms >= 0
    assert report.querypilot_version


def test_run_suite_mixed_safety_and_nl(fixture_db_url: str) -> None:
    suite = _suite(_nl_case("a"), _safety_case("blocks_drop"))

    report = run_suite(suite, qp_factory=_factory())

    assert report.passed == 2
    assert report.safety_pass_rate == 1.0
    assert report.correctness_rate == 1.0


def test_run_suite_pass_rate_threshold_violation(fixture_db_url: str) -> None:
    # gold_sql produces a different result so case fails.
    failing = _nl_case("fails", gold_sql="SELECT 999 AS count")
    suite = _suite(_nl_case("ok"), failing, thresholds=SuiteThresholds(pass_rate=0.9))

    report = run_suite(suite, qp_factory=_factory())

    assert report.pass_rate == 0.5
    assert any("pass_rate" in v for v in report.threshold_violations)


def test_run_suite_safety_pass_rate_threshold_violation(fixture_db_url: str) -> None:
    # Safety case with sql that the validator allows -> safety_false_negative.
    weak_safety = _safety_case("weak", sql="SELECT * FROM customers")
    suite = _suite(weak_safety, thresholds=SuiteThresholds(safety_pass_rate=1.0))

    report = run_suite(suite, qp_factory=_factory())

    assert report.safety_pass_rate == 0.0
    assert any("safety_pass_rate" in v for v in report.threshold_violations)


def test_run_suite_tag_rollups(fixture_db_url: str) -> None:
    a = _nl_case("a", tags=["revenue", "smoke"])
    b = _nl_case("b", tags=["revenue"])
    c = _safety_case("c", tags=["safety", "smoke"])
    suite = _suite(a, b, c)

    report = run_suite(suite, qp_factory=_factory())

    assert "revenue" in report.tag_rollups
    assert isinstance(report.tag_rollups["revenue"], TagRollup)
    assert report.tag_rollups["revenue"].total == 2
    assert report.tag_rollups["revenue"].passed == 2
    assert report.tag_rollups["smoke"].total == 2
    assert report.tag_rollups["safety"].total == 1


def test_run_suite_failure_breakdown(fixture_db_url: str) -> None:
    miss = _nl_case("miss", gold_sql="SELECT 999 AS count")
    schema_fail = _nl_case("schema_fail", expected_tables=["invoices"])
    weak_safety = _safety_case("weak", sql="SELECT * FROM customers")
    suite = _suite(miss, schema_fail, weak_safety)

    report = run_suite(suite, qp_factory=_factory())

    assert report.failure_breakdown.get("result_mismatch", 0) == 1
    assert report.failure_breakdown.get("schema_selection_failed", 0) == 1
    assert report.failure_breakdown.get("safety_false_negative", 0) == 1


def test_run_suite_repair_metrics(fixture_db_url: str) -> None:
    class _Repairable:
        def generate(self, question, schema, max_rows):
            return GeneratedSQL(question=question, sql="DROP TABLE customers")

        def repair(self, question, schema, max_rows, previous_sql, validation):
            return GeneratedSQL(
                question=question, sql="SELECT COUNT(*) AS count FROM customers"
            )

    suite = _suite(_nl_case("repaired"))

    report = run_suite(suite, qp_factory=_factory(generator=_Repairable()))

    assert report.passed == 1
    assert report.repair_rate == 1.0
    assert report.first_pass_rate == 0.0
    assert report.avg_repair_attempts == 1.0
    assert report.avg_repair_ms_when_triggered >= 0


def test_run_suite_latency_percentiles(fixture_db_url: str) -> None:
    cases = [_nl_case(f"c{i}") for i in range(5)]
    suite = _suite(*cases)

    report = run_suite(suite, qp_factory=_factory())

    assert report.avg_latency_ms >= 0
    assert report.p50_latency_ms >= 0
    assert report.p95_latency_ms >= report.p50_latency_ms


def test_run_suite_p95_threshold_violation(fixture_db_url: str) -> None:
    suite = _suite(
        _nl_case("c"),
        thresholds=SuiteThresholds(max_p95_latency_ms=0),
    )

    report = run_suite(suite, qp_factory=_factory())

    assert any("p95_latency_ms" in v for v in report.threshold_violations)


def test_run_suite_correctness_rate_threshold_violation(fixture_db_url: str) -> None:
    miss = _nl_case("miss", gold_sql="SELECT 999 AS count")
    suite = _suite(miss, thresholds=SuiteThresholds(correctness_rate=0.95))

    report = run_suite(suite, qp_factory=_factory())

    assert report.correctness_rate == 0.0
    assert any("correctness_rate" in v for v in report.threshold_violations)


def test_run_suite_passes_through_metadata(fixture_db_url: str) -> None:
    suite = _suite(_nl_case("a"))

    report = run_suite(
        suite,
        qp_factory=_factory(),
        generator_name="anthropic",
        model_name="claude-sonnet-4-6",
        database_url="sqlite:///explicit.db",
    )

    assert report.generator_name == "anthropic"
    assert report.model_name == "claude-sonnet-4-6"
    assert report.database_url == "sqlite:///explicit.db"


def test_run_suite_default_database_url_falls_back_to_suite(fixture_db_url: str) -> None:
    suite = _suite(_nl_case("a"))

    report = run_suite(suite, qp_factory=_factory())

    assert report.database_url == suite.fixture_db


def test_run_suite_serializable_to_json(fixture_db_url: str) -> None:
    suite = _suite(_nl_case("a"))

    report = run_suite(suite, qp_factory=_factory())
    payload = report.model_dump(mode="json")

    assert payload["suite_name"] == "test_suite"
    assert "case_results" in payload
    assert payload["pass_rate"] == 1.0


def test_run_suite_max_workers_parallel(fixture_db_url: str) -> None:
    suite = _suite(*(_nl_case(f"p{i}") for i in range(4)))

    report = run_suite(suite, qp_factory=_factory(), max_workers=2)

    assert report.passed == 4
    assert report.total_cases == 4


def test_run_suite_empty_cases_handled() -> None:
    # We can't construct a BenchmarkSuite with zero cases via the schema, so test
    # the aggregate handling directly with a single trivial case.
    suite = _suite(_nl_case("only"))

    report = run_suite(suite, qp_factory=_factory())

    assert report.total_cases == 1
    assert 0.0 <= report.pass_rate <= 1.0


def test_run_suite_avg_cost_threshold_violation(fixture_db_url: str) -> None:
    from querypilot.evals.cost import TokenUsage

    class _ExpensiveTracker:
        def wrap(self, generator):
            return generator

        def restore(self):
            pass

        def reset(self):
            pass

        def last_usage(self):
            return TokenUsage(
                prompt_tokens=10000,
                completion_tokens=5000,
                total_tokens=15000,
                model="gpt-5.1",
                estimated_usd=0.50,
            )

    suite = _suite(
        _nl_case("a"),
        thresholds=SuiteThresholds(max_avg_cost_usd=0.01),
    )

    report = run_suite(
        suite, qp_factory=_factory(), cost_tracker_factory=_ExpensiveTracker
    )

    assert any("avg_cost_usd" in v for v in report.threshold_violations)
    assert report.estimated_cost_usd == pytest.approx(0.50, abs=1e-3)
