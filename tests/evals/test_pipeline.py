from __future__ import annotations

from pathlib import Path

import pytest

from querypilot import QueryPilot
from querypilot.core.types import GeneratedSQL
from querypilot.evals import (
    BenchmarkCase,
    CaseResult,
    FailureCategory,
    run_case,
)


FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "demo.db"


@pytest.fixture(scope="module")
def fixture_db_url() -> str:
    if not FIXTURE_DB.exists():
        from tests.fixtures.seed_demo import seed

        seed(FIXTURE_DB)
    return f"sqlite:///{FIXTURE_DB}"


def _factory(database_url: str, generator=None):
    def _make(case: BenchmarkCase) -> QueryPilot:
        url = case.fixture_db or database_url
        return QueryPilot.connect(database_url=url, dialect="sqlite", generator=generator)

    return _make


def _case(**overrides) -> BenchmarkCase:
    base = {
        "id": "c1",
        "question": "Count of customers",
        "gold_sql": "SELECT COUNT(*) AS count FROM customers",
        "expected_tables": ["customers"],
        "tags": ["smoke"],
    }
    base.update(overrides)
    base["fixture_db"] = base.get("fixture_db", "sqlite:///" + str(FIXTURE_DB))
    return BenchmarkCase(**base)


def test_demo_generator_count_passes(fixture_db_url: str) -> None:
    case = _case()

    result = run_case(case, _factory(fixture_db_url))

    assert isinstance(result, CaseResult)
    assert result.passed is True
    assert result.failure_category is None
    assert result.correctness_passed is True
    assert result.validation_passed is True
    assert result.execution_passed is True
    assert result.repair_attempts == 0
    assert result.rowset_match is not None
    assert result.rowset_match.matched is True
    assert result.timings.total_ms >= 0
    assert result.token_usage is None  # NullCostTracker


def test_safety_case_drop_table_passes(fixture_db_url: str) -> None:
    case = BenchmarkCase(
        id="blocks_drop_table",
        sql="DROP TABLE customers",
        should_pass=False,
        expected_failure_kind="validation",
        expected_error_contains=["Only SELECT"],
        fixture_db=f"sqlite:///{FIXTURE_DB}",
        tags=["safety"],
    )

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is True
    assert result.safety_passed is True
    assert result.failure_category is None
    assert result.validation_passed is False
    assert any("Only SELECT" in e for e in result.validation_errors)


def test_safety_false_negative_when_validator_passes(fixture_db_url: str) -> None:
    case = BenchmarkCase(
        id="false_negative",
        sql="SELECT * FROM customers",
        should_pass=False,
        expected_failure_kind="validation",
        fixture_db=f"sqlite:///{FIXTURE_DB}",
        tags=["safety"],
    )

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is False
    assert result.failure_category == FailureCategory.SAFETY_FALSE_NEGATIVE
    assert result.safety_passed is False
    assert result.validation_passed is True


def test_generation_failed_when_demo_generator_returns_no_sql(fixture_db_url: str) -> None:
    case = _case(question="Tell me about the weather", gold_sql=None, expected_tables=[])

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is False
    assert result.failure_category == FailureCategory.GENERATION_FAILED
    assert result.candidate_sql is None
    assert result.error is not None


def test_result_mismatch_detected(fixture_db_url: str) -> None:
    # Demo generator answers "how many customers" with COUNT(*) → returns 5.
    # Make the gold SQL return a different value to force a mismatch.
    case = _case(gold_sql="SELECT 999 AS count")

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is False
    assert result.failure_category == FailureCategory.RESULT_MISMATCH
    assert result.execution_passed is True
    assert result.rowset_match is not None
    assert result.rowset_match.matched is False


def test_schema_selection_failed(fixture_db_url: str) -> None:
    case = _case(expected_tables=["invoices"])  # demo generator picks "customers"

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is False
    assert result.failure_category == FailureCategory.SCHEMA_SELECTION_FAILED
    assert "invoices" in (result.error or "")


def test_must_include_violation_reported_as_result_mismatch(fixture_db_url: str) -> None:
    case = _case(must_include=["GROUP BY"])  # demo COUNT query has no GROUP BY

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is False
    assert result.failure_category == FailureCategory.RESULT_MISMATCH
    assert "GROUP BY" in (result.error or "")


def test_must_not_contain_violation_reported_as_result_mismatch(fixture_db_url: str) -> None:
    case = _case(must_not_contain=["count"])  # demo COUNT query contains COUNT

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is False
    assert result.failure_category == FailureCategory.RESULT_MISMATCH
    assert "count" in (result.error or "").lower()


def test_repair_failed_when_generator_does_not_repair(fixture_db_url: str) -> None:
    class _BadGenerator:
        def generate(self, question, schema, max_rows):
            return GeneratedSQL(question=question, sql="DROP TABLE customers")

    case = _case()
    result = run_case(case, _factory(fixture_db_url, generator=_BadGenerator()))

    assert result.passed is False
    assert result.failure_category == FailureCategory.VALIDATION_FAILED
    assert result.repair_attempts == 0


def test_repair_loop_invoked_when_generator_supports_it(fixture_db_url: str) -> None:
    class _RepairableGenerator:
        def __init__(self) -> None:
            self.calls = 0
            self.repaired = 0

        def generate(self, question, schema, max_rows):
            self.calls += 1
            return GeneratedSQL(question=question, sql="DROP TABLE customers")

        def repair(self, question, schema, max_rows, previous_sql, validation):
            self.repaired += 1
            return GeneratedSQL(
                question=question,
                sql="SELECT COUNT(*) AS count FROM customers",
            )

    gen = _RepairableGenerator()
    case = _case()
    result = run_case(case, _factory(fixture_db_url, generator=gen))

    assert result.passed is True
    assert result.repair_attempts == 1
    assert gen.repaired == 1
    assert result.timings.repair_ms >= 0
    # candidate_sql must reflect the *repaired* SQL, not the original DROP TABLE
    assert result.candidate_sql is not None
    assert "drop" not in result.candidate_sql.lower()
    assert "count" in result.candidate_sql.lower()


def test_invalid_multitable_column_fails_validation(fixture_db_url: str) -> None:
    class _BadColumnGenerator:
        def generate(self, question, schema, max_rows):
            return GeneratedSQL(
                question=question,
                sql=(
                    "SELECT customers.x_does_not_exist FROM customers "
                    "JOIN invoices ON customers.id = invoices.customer_id LIMIT 1"
                ),
            )

    case = _case()
    result = run_case(case, _factory(fixture_db_url, generator=_BadColumnGenerator()))

    assert result.passed is False
    assert result.failure_category == FailureCategory.VALIDATION_FAILED
    assert result.validation_errors == ["Unknown column: x_does_not_exist"]


def test_unknown_error_when_gold_sql_is_invalid(fixture_db_url: str) -> None:
    case = _case(gold_sql="SELECT * FROM no_such_table")

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is False
    assert result.failure_category == FailureCategory.UNKNOWN_ERROR
    assert "gold_sql" in (result.error or "")


def test_passes_without_gold_sql(fixture_db_url: str) -> None:
    case = _case(gold_sql=None)

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is True
    assert result.correctness_passed is False  # no gold to compare
    assert result.execution_passed is True


def test_token_usage_reported_when_cost_tracker_provided(fixture_db_url: str) -> None:
    class _RecordingTracker:
        def __init__(self) -> None:
            self.reset_called = False
            self.restore_called = False

        def wrap(self, generator):
            return generator

        def restore(self):
            self.restore_called = True

        def last_usage(self):
            from querypilot.evals.cost import TokenUsage

            return TokenUsage(
                prompt_tokens=12,
                completion_tokens=4,
                total_tokens=16,
                model="demo-model",
                estimated_usd=0.0001,
            )

        def reset(self):
            self.reset_called = True

    tracker = _RecordingTracker()
    case = _case()
    result = run_case(case, _factory(fixture_db_url), cost_tracker=tracker)

    assert tracker.reset_called is True
    assert tracker.restore_called is True
    assert result.token_usage is not None
    assert result.token_usage.prompt_tokens == 12
    assert result.estimated_cost_usd == 0.0001


def test_null_cost_tracker_used_by_default(fixture_db_url: str) -> None:
    case = _case()

    result = run_case(case, _factory(fixture_db_url))

    assert result.token_usage is None
    assert result.estimated_cost_usd is None


def test_audit_trail_isolated_to_eval_scope(fixture_db_url: str) -> None:
    case = _case()
    factory = _factory(fixture_db_url)

    captured: list = []

    def tracking_factory(c):
        qp = factory(c)
        captured.append(qp)
        return qp

    result = run_case(case, tracking_factory)

    assert result.passed is True
    qp = captured[0]
    records = qp.audit_sink.recent(50)
    assert all(r.app_name == "querypilot.eval" for r in records if r.app_name)
    assert all(r.trace_id == case.id for r in records if r.trace_id)


def test_unknown_error_when_factory_raises(fixture_db_url: str) -> None:
    def boom(case: BenchmarkCase) -> QueryPilot:
        raise RuntimeError("factory failed")

    case = _case()
    result = run_case(case, boom)

    assert result.passed is False
    assert result.failure_category == FailureCategory.UNKNOWN_ERROR
    assert "factory failed" in (result.error or "")
    assert result.traceback is not None


def test_total_ms_populated_on_failure(fixture_db_url: str) -> None:
    case = _case(gold_sql="SELECT 999 AS count")

    result = run_case(case, _factory(fixture_db_url))

    assert result.passed is False
    assert result.timings.total_ms >= 0
    assert result.timings.execute_ms >= 0


def test_serializable_to_json(fixture_db_url: str) -> None:
    case = _case()
    result = run_case(case, _factory(fixture_db_url))

    payload = result.model_dump(mode="json")

    assert payload["passed"] is True
    assert "timings" in payload
    assert payload["tags"] == ["smoke"]
