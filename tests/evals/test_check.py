from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from querypilot.evals import (
    CaseResult,
    FailureCategory,
    StageTimings,
    SuiteReport,
    SuiteThresholds,
    check_report,
    format_outcome,
    load_report,
    write_outcome,
)


def _stub_report(
    *,
    pass_rate: float = 1.0,
    safety_pass_rate: float = 1.0,
    correctness_rate: float = 1.0,
    p95_latency_ms: int = 200,
    estimated_cost_usd: float = 0.0,
    case_results: list[CaseResult] | None = None,
    threshold_violations: list[str] | None = None,
) -> SuiteReport:
    cases = case_results if case_results is not None else [
        CaseResult(id="c1", passed=True, timings=StageTimings(total_ms=100)),
    ]
    return SuiteReport(
        suite_name="smoke",
        querypilot_version="0.1.0",
        started_at=datetime(2026, 4, 27, 14, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 4, 27, 14, 0, 1, tzinfo=UTC),
        duration_ms=1000,
        total_cases=len(cases),
        passed=sum(1 for c in cases if c.passed),
        failed=sum(1 for c in cases if not c.passed),
        pass_rate=pass_rate,
        safety_pass_rate=safety_pass_rate,
        correctness_rate=correctness_rate,
        repair_rate=0.0,
        first_pass_rate=pass_rate,
        avg_repair_attempts=0.0,
        avg_latency_ms=100,
        p50_latency_ms=100,
        p95_latency_ms=p95_latency_ms,
        avg_generate_ms=10,
        avg_validate_ms=5,
        avg_repair_ms_when_triggered=0,
        avg_execute_ms=5,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        estimated_cost_usd=estimated_cost_usd,
        tag_rollups={},
        failure_breakdown={},
        thresholds=SuiteThresholds(),
        threshold_violations=threshold_violations or [],
        case_results=cases,
    )


def test_check_no_thresholds_no_baseline_passes() -> None:
    report = _stub_report()

    outcome = check_report(report)

    assert outcome.ok is True
    assert outcome.reasons == []
    assert outcome.regressed_cases == []


def test_check_pass_rate_below_threshold_fails() -> None:
    report = _stub_report(pass_rate=0.5)

    outcome = check_report(report, threshold=0.9)

    assert outcome.ok is False
    assert any("pass_rate" in r for r in outcome.reasons)


def test_check_pass_rate_at_threshold_passes() -> None:
    report = _stub_report(pass_rate=0.9)

    outcome = check_report(report, threshold=0.9)

    assert outcome.ok is True


def test_check_safety_threshold() -> None:
    report = _stub_report(safety_pass_rate=0.5)

    outcome = check_report(report, require_safety_pass_rate=1.0)

    assert outcome.ok is False
    assert any("safety_pass_rate" in r for r in outcome.reasons)


def test_check_correctness_threshold() -> None:
    report = _stub_report(correctness_rate=0.5)

    outcome = check_report(report, require_correctness_rate=0.9)

    assert outcome.ok is False
    assert any("correctness_rate" in r for r in outcome.reasons)


def test_check_max_p95_threshold() -> None:
    report = _stub_report(p95_latency_ms=500)

    outcome = check_report(report, max_p95_ms=200)

    assert outcome.ok is False
    assert any("p95_latency_ms" in r for r in outcome.reasons)


def test_check_propagates_existing_threshold_violations() -> None:
    report = _stub_report(
        threshold_violations=["pass_rate 0.5 below threshold 0.9"]
    )

    outcome = check_report(report)

    assert outcome.ok is False
    assert "pass_rate 0.5 below threshold 0.9" in outcome.reasons


def test_check_baseline_pass_rate_drop() -> None:
    baseline = _stub_report(pass_rate=0.95)
    current = _stub_report(pass_rate=0.85)

    outcome = check_report(current, baseline=baseline)

    assert outcome.ok is False
    assert any("pass_rate dropped" in r for r in outcome.reasons)
    assert outcome.baseline is not None
    assert outcome.baseline.pass_rate == 0.95


def test_check_baseline_safety_drop() -> None:
    baseline = _stub_report(safety_pass_rate=1.0)
    current = _stub_report(safety_pass_rate=0.8)

    outcome = check_report(current, baseline=baseline)

    assert outcome.ok is False
    assert any("safety_pass_rate dropped" in r for r in outcome.reasons)


def test_check_baseline_correctness_drop() -> None:
    baseline = _stub_report(correctness_rate=0.95)
    current = _stub_report(correctness_rate=0.7)

    outcome = check_report(current, baseline=baseline)

    assert outcome.ok is False
    assert any("correctness_rate dropped" in r for r in outcome.reasons)


def test_check_regressed_cases_listed() -> None:
    baseline = _stub_report(
        case_results=[
            CaseResult(id="a", passed=True, timings=StageTimings()),
            CaseResult(id="b", passed=True, timings=StageTimings()),
        ]
    )
    current = _stub_report(
        case_results=[
            CaseResult(id="a", passed=True, timings=StageTimings()),
            CaseResult(
                id="b",
                passed=False,
                failure_category=FailureCategory.RESULT_MISMATCH,
                timings=StageTimings(),
            ),
        ],
        pass_rate=0.5,
    )

    outcome = check_report(current, baseline=baseline)

    assert outcome.ok is False
    regressed = {r.id for r in outcome.regressed_cases}
    assert regressed == {"b"}
    [b] = outcome.regressed_cases
    assert b.was_passing is True
    assert b.now_passing is False
    assert b.current_failure_category == "result_mismatch"


def test_check_case_only_in_current_is_not_regression() -> None:
    baseline = _stub_report(case_results=[CaseResult(id="a", passed=True, timings=StageTimings())])
    current = _stub_report(
        case_results=[
            CaseResult(id="a", passed=True, timings=StageTimings()),
            CaseResult(id="b", passed=False, timings=StageTimings()),
        ]
    )

    outcome = check_report(current, baseline=baseline)

    # b is new, not a regression
    assert outcome.regressed_cases == []


def test_check_latency_delta_recorded() -> None:
    baseline = _stub_report(p95_latency_ms=100)
    current = _stub_report(p95_latency_ms=300)

    outcome = check_report(current, baseline=baseline)

    assert outcome.latency_p95_delta_ms == 200


def test_check_latency_delta_negative_is_improvement() -> None:
    baseline = _stub_report(p95_latency_ms=300)
    current = _stub_report(p95_latency_ms=100)

    outcome = check_report(current, baseline=baseline)

    assert outcome.latency_p95_delta_ms == -200
    # No reason to fail just because latency improved
    assert all("p95" not in r for r in outcome.reasons)


def test_check_outcome_ok_when_baseline_present_and_no_drop() -> None:
    baseline = _stub_report()
    current = _stub_report()

    outcome = check_report(current, baseline=baseline)

    assert outcome.ok is True


def test_format_outcome_ok_includes_pass_rate() -> None:
    outcome = check_report(_stub_report())

    text = format_outcome(outcome)

    assert "OK" in text
    assert "pass_rate" in text


def test_format_outcome_failure_lists_reasons() -> None:
    outcome = check_report(
        _stub_report(pass_rate=0.5),
        threshold=0.9,
    )

    text = format_outcome(outcome)

    assert "Regression detected" in text
    assert "pass_rate" in text


def test_format_outcome_lists_regressed_cases() -> None:
    baseline = _stub_report(
        case_results=[CaseResult(id="x", passed=True, timings=StageTimings())]
    )
    current = _stub_report(
        case_results=[
            CaseResult(
                id="x",
                passed=False,
                failure_category=FailureCategory.RESULT_MISMATCH,
                timings=StageTimings(),
            )
        ],
        pass_rate=0.0,
    )

    text = format_outcome(check_report(current, baseline=baseline))

    assert "Failed cases" in text
    assert "x" in text
    assert "result_mismatch" in text


def test_format_outcome_renders_zero_latency_delta() -> None:
    baseline = _stub_report(p95_latency_ms=100)
    current = _stub_report(p95_latency_ms=100, pass_rate=0.5)

    text = format_outcome(check_report(current, baseline=baseline, threshold=0.9))

    assert "(+0 ms)" in text


def test_format_outcome_includes_latency_section() -> None:
    baseline = _stub_report(p95_latency_ms=100)
    current = _stub_report(p95_latency_ms=300, pass_rate=0.5)

    text = format_outcome(check_report(current, baseline=baseline, threshold=0.9))

    assert "Latency" in text
    assert "100" in text
    assert "300" in text


def test_load_report_round_trips_via_write_json(tmp_path: Path) -> None:
    from querypilot.evals import write_json

    report = _stub_report()
    out = tmp_path / "report.json"
    write_json(report, out)

    loaded = load_report(out)

    assert loaded.suite_name == report.suite_name
    assert loaded.pass_rate == report.pass_rate


def test_load_report_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_report(tmp_path / "missing.json")


def test_write_outcome_creates_parent_dirs(tmp_path: Path) -> None:
    outcome = check_report(_stub_report())
    target = tmp_path / "nested" / "outcome.json"

    written = write_outcome(outcome, target)

    payload = json.loads(written.read_text())
    assert payload["ok"] is True


def test_check_outcome_serializable_to_json() -> None:
    outcome = check_report(_stub_report())

    payload = outcome.model_dump(mode="json")

    assert payload["ok"] is True
    assert "current" in payload
