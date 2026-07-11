from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


from querypilot.evals import (
    CaseResult,
    StageTimings,
    SuiteReport,
    SuiteThresholds,
    TagRollup,
    render_terminal,
    write_json,
)


def _stub_report(
    *,
    case_results: list[CaseResult] | None = None,
    thresholds: SuiteThresholds | None = None,
    threshold_violations: list[str] | None = None,
    failure_breakdown: dict[str, int] | None = None,
    tag_rollups: dict[str, TagRollup] | None = None,
    pass_rate: float = 1.0,
    safety_pass_rate: float = 1.0,
    correctness_rate: float = 1.0,
    p95_latency_ms: int = 220,
    estimated_cost_usd: float = 0.0,
) -> SuiteReport:
    cases = case_results if case_results is not None else [
        CaseResult(
            id="c1",
            passed=True,
            correctness_passed=True,
            validation_passed=True,
            execution_passed=True,
            timings=StageTimings(total_ms=100, generate_ms=80, validate_ms=10, execute_ms=10),
            tags=["smoke"],
        )
    ]
    return SuiteReport(
        suite_name="smoke",
        generator_name="anthropic",
        model_name="claude-sonnet-4-6",
        database_url="sqlite:///fixtures/demo.db",
        querypilot_version="0.1.0",
        started_at=datetime(2026, 4, 27, 14, 22, 8, tzinfo=UTC),
        finished_at=datetime(2026, 4, 27, 14, 22, 12, tzinfo=UTC),
        duration_ms=4700,
        total_cases=len(cases),
        passed=sum(1 for c in cases if c.passed),
        failed=sum(1 for c in cases if not c.passed),
        pass_rate=pass_rate,
        safety_pass_rate=safety_pass_rate,
        correctness_rate=correctness_rate,
        repair_rate=0.17,
        first_pass_rate=0.83,
        avg_repair_attempts=0.4,
        avg_latency_ms=143,
        p50_latency_ms=110,
        p95_latency_ms=p95_latency_ms,
        avg_generate_ms=108,
        avg_validate_ms=3,
        avg_repair_ms_when_triggered=27,
        avg_execute_ms=1,
        total_prompt_tokens=21450,
        total_completion_tokens=4880,
        estimated_cost_usd=estimated_cost_usd,
        tag_rollups=tag_rollups
        or {
            "revenue": TagRollup(tag="revenue", total=4, passed=4, pass_rate=1.0),
            "joins": TagRollup(tag="joins", total=3, passed=3, pass_rate=1.0),
        },
        failure_breakdown=failure_breakdown or {},
        thresholds=thresholds or SuiteThresholds(),
        threshold_violations=threshold_violations or [],
        case_results=cases,
    )


def test_write_json_writes_full_report(tmp_path: Path) -> None:
    report = _stub_report()
    out = tmp_path / "report.json"

    write_json(report, out)
    payload = json.loads(out.read_text())

    assert payload["suite_name"] == "smoke"
    assert payload["pass_rate"] == 1.0
    assert payload["tag_rollups"]["revenue"]["passed"] == 4
    assert "case_results" in payload


def test_write_json_creates_parent_dirs(tmp_path: Path) -> None:
    report = _stub_report()
    out = tmp_path / "nested" / "subdir" / "report.json"

    written = write_json(report, out)

    assert written.exists()


def test_render_terminal_includes_all_sections() -> None:
    report = _stub_report()

    text = render_terminal(report, color=False)

    assert "QueryPilot Eval Report" in text
    assert "Suite:     smoke" in text
    assert "Generator: anthropic (claude-sonnet-4-6)" in text
    assert "Database:  sqlite:///fixtures/demo.db" in text
    assert "Overall" in text
    assert "Pass rate" in text
    assert "Tag rollups" in text
    assert "revenue" in text
    assert "Failure breakdown" in text
    assert "(none)" in text
    assert "Repair summary" in text
    assert "Latency & cost" in text
    assert "No threshold violations" in text


def test_render_terminal_shows_failure_breakdown() -> None:
    report = _stub_report(
        failure_breakdown={"result_mismatch": 2, "validation_failed": 1},
        pass_rate=0.5,
    )

    text = render_terminal(report, color=False)

    assert "result_mismatch" in text
    assert "validation_failed" in text


def test_render_terminal_shows_threshold_violations() -> None:
    report = _stub_report(
        thresholds=SuiteThresholds(pass_rate=0.9),
        threshold_violations=["pass_rate 0.500 below threshold 0.900"],
        pass_rate=0.5,
    )

    text = render_terminal(report, color=False)

    assert "Threshold violations" in text
    assert "pass_rate 0.500 below threshold 0.900" in text


def test_render_terminal_marks_pass_rate_below_threshold_with_cross() -> None:
    report = _stub_report(
        thresholds=SuiteThresholds(pass_rate=0.9),
        pass_rate=0.5,
    )

    text = render_terminal(report, color=False)

    # Cross mark "❌" should appear next to Pass rate row
    assert "❌" in text


def test_render_terminal_no_color_when_color_false() -> None:
    report = _stub_report()

    text = render_terminal(report, color=False)

    assert "\033[" not in text


def test_render_terminal_with_color_includes_ansi_codes() -> None:
    report = _stub_report()

    text = render_terminal(report, color=True)

    assert "\033[32m" in text  # green


def test_render_terminal_p95_latency_threshold_marker() -> None:
    report = _stub_report(
        thresholds=SuiteThresholds(max_p95_latency_ms=100),
        p95_latency_ms=500,
    )

    text = render_terminal(report, color=False)

    # P95 line should show cross because 500 > 100
    assert "P95 latency" in text
    p95_line = next(line for line in text.split("\n") if "P95 latency" in line)
    assert "❌" in p95_line


def test_render_terminal_handles_empty_tag_rollups() -> None:
    report = _stub_report(tag_rollups={})

    text = render_terminal(report, color=False)

    assert "Tag rollups" in text
    assert "(none)" in text


def test_render_terminal_includes_estimated_cost() -> None:
    report = _stub_report(estimated_cost_usd=1.2345)

    text = render_terminal(report, color=False)

    assert "$1.23" in text or "$1.2345" in text


def test_render_terminal_snapshot() -> None:
    report = _stub_report()

    text = render_terminal(report, color=False)

    expected_lines = [
        "QueryPilot Eval Report",
        "Suite:     smoke",
        "Generator: anthropic (claude-sonnet-4-6)",
        "Database:  sqlite:///fixtures/demo.db",
        "Started:   2026-04-27 14:22:08Z",
        "Duration:  4.7s",
    ]
    for line in expected_lines:
        assert line in text


def test_render_terminal_handles_missing_metadata() -> None:
    cases = [
        CaseResult(
            id="x",
            passed=True,
            timings=StageTimings(total_ms=10),
        )
    ]
    report = SuiteReport(
        suite_name="bare",
        querypilot_version="0.1.0",
        started_at=datetime(2026, 4, 27, 14, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 4, 27, 14, 0, 1, tzinfo=UTC),
        duration_ms=1000,
        total_cases=1,
        passed=1,
        failed=0,
        pass_rate=1.0,
        safety_pass_rate=1.0,
        correctness_rate=1.0,
        repair_rate=0.0,
        first_pass_rate=1.0,
        avg_repair_attempts=0.0,
        avg_latency_ms=10,
        p50_latency_ms=10,
        p95_latency_ms=10,
        avg_generate_ms=0,
        avg_validate_ms=0,
        avg_repair_ms_when_triggered=0,
        avg_execute_ms=0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        estimated_cost_usd=0.0,
        tag_rollups={},
        failure_breakdown={},
        thresholds=SuiteThresholds(),
        threshold_violations=[],
        case_results=cases,
    )

    text = render_terminal(report, color=False)

    # No model_name or generator_name - should show n/a
    assert "Generator: n/a" in text
