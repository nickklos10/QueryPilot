from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from querypilot.evals import (
    CaseResult,
    Leaderboard,
    LeaderboardError,
    StageTimings,
    SuiteReport,
    SuiteThresholds,
    build_leaderboard,
)
from querypilot.evals.leaderboard import (
    load_leaderboard,
    render_markdown,
    render_terminal,
    write_json,
    write_markdown,
)


def _report(
    *,
    suite_name: str = "smoke",
    generator_name: str | None = "anthropic",
    model_name: str | None = "claude-opus-4-8",
    total_cases: int = 7,
    passed: int = 7,
    pass_rate: float = 1.0,
    safety_pass_rate: float = 1.0,
    correctness_rate: float = 1.0,
    repair_rate: float = 0.1,
    first_pass_rate: float = 0.9,
    p50_latency_ms: int = 900,
    p95_latency_ms: int = 2400,
    estimated_cost_usd: float = 18.0,
    total_prompt_tokens: int = 21450,
    total_completion_tokens: int = 4880,
) -> SuiteReport:
    cases = [
        CaseResult(
            id=f"c{i}",
            passed=True,
            correctness_passed=True,
            validation_passed=True,
            execution_passed=True,
            timings=StageTimings(total_ms=100),
            tags=["smoke"],
        )
        for i in range(total_cases)
    ]
    return SuiteReport(
        suite_name=suite_name,
        generator_name=generator_name,
        model_name=model_name,
        database_url="sqlite:///fixtures/demo.db",
        querypilot_version="0.1.1",
        started_at=datetime(2026, 7, 11, 14, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 11, 14, 0, 4, tzinfo=UTC),
        duration_ms=4000,
        total_cases=total_cases,
        passed=passed,
        failed=total_cases - passed,
        pass_rate=pass_rate,
        safety_pass_rate=safety_pass_rate,
        correctness_rate=correctness_rate,
        repair_rate=repair_rate,
        first_pass_rate=first_pass_rate,
        avg_repair_attempts=0.2,
        avg_latency_ms=1000,
        p50_latency_ms=p50_latency_ms,
        p95_latency_ms=p95_latency_ms,
        avg_generate_ms=800,
        avg_validate_ms=3,
        avg_repair_ms_when_triggered=27,
        avg_execute_ms=1,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        estimated_cost_usd=estimated_cost_usd,
        tag_rollups={},
        failure_breakdown={},
        thresholds=SuiteThresholds(),
        threshold_violations=[],
        case_results=cases,
    )


def test_ranks_by_pass_rate_descending() -> None:
    reports = [
        _report(model_name="haiku", pass_rate=0.78),
        _report(model_name="opus", pass_rate=0.95),
        _report(model_name="sonnet", pass_rate=0.86),
    ]

    board = build_leaderboard(reports)

    assert [e.model_name for e in board.entries] == ["opus", "sonnet", "haiku"]
    assert [e.rank for e in board.entries] == [1, 2, 3]


def test_tiebreak_safety_then_cost() -> None:
    reports = [
        # Same pass rate; lower safety loses.
        _report(model_name="risky", pass_rate=0.9, safety_pass_rate=0.8, estimated_cost_usd=1.0),
        _report(model_name="safe-cheap", pass_rate=0.9, safety_pass_rate=1.0, estimated_cost_usd=5.0),
        _report(model_name="safe-cheaper", pass_rate=0.9, safety_pass_rate=1.0, estimated_cost_usd=2.0),
    ]

    board = build_leaderboard(reports)

    # Safety wins first, then cheaper cost breaks the safe tie.
    assert [e.model_name for e in board.entries] == [
        "safe-cheaper",
        "safe-cheap",
        "risky",
    ]


def test_refuses_mixed_suites_without_force() -> None:
    reports = [_report(suite_name="smoke"), _report(suite_name="safety")]

    with pytest.raises(LeaderboardError, match="multiple suites"):
        build_leaderboard(reports)


def test_allows_mixed_suites_with_force_and_warns() -> None:
    reports = [_report(suite_name="smoke"), _report(suite_name="safety")]

    board = build_leaderboard(reports, force=True)

    assert board.suite_name == "(mixed)"
    assert any("multiple suites" in w for w in board.warnings)
    assert len(board.entries) == 2


def test_warns_on_case_count_mismatch_without_refusing() -> None:
    reports = [
        _report(total_cases=7, passed=7),
        _report(total_cases=5, passed=5, model_name="sonnet"),
    ]

    board = build_leaderboard(reports)

    assert any("case count" in w for w in board.warnings)
    assert len(board.entries) == 2


def test_labels_override_applied_in_report_order() -> None:
    reports = [
        _report(model_name="opus", pass_rate=0.95),
        _report(model_name="haiku", pass_rate=0.78),
    ]

    board = build_leaderboard(reports, labels=["Opus 4.8", "Haiku 4.5"])

    # Opus ranks first regardless of input order; its override label follows it.
    assert board.entries[0].label == "Opus 4.8"
    assert board.entries[1].label == "Haiku 4.5"


def test_labels_count_mismatch_raises() -> None:
    reports = [_report(), _report(model_name="sonnet")]

    with pytest.raises(LeaderboardError, match="does not match report count"):
        build_leaderboard(reports, labels=["only-one"])


def test_empty_reports_raises() -> None:
    with pytest.raises(LeaderboardError, match="At least one report"):
        build_leaderboard([])


def test_default_label_for_labelless_report_backward_compat() -> None:
    # An old report predating generator/model metadata still loads and ranks.
    report = _report(generator_name=None, model_name=None)

    board = build_leaderboard([report])

    assert board.entries[0].label == "n/a"


def test_default_label_uses_generator_when_no_model() -> None:
    report = _report(generator_name="demo", model_name=None)

    board = build_leaderboard([report])

    assert board.entries[0].label == "demo"


def test_total_tokens_is_sum_of_prompt_and_completion() -> None:
    report = _report(total_prompt_tokens=1000, total_completion_tokens=250)

    board = build_leaderboard([report])

    assert board.entries[0].total_tokens == 1250


def test_render_markdown_snapshot() -> None:
    reports = [
        _report(model_name="opus", pass_rate=1.0, safety_pass_rate=1.0),
        _report(
            model_name="risky",
            pass_rate=0.86,
            safety_pass_rate=0.9,
            estimated_cost_usd=5.9,
        ),
    ]

    md = render_markdown(build_leaderboard(reports))
    lines = md.strip().split("\n")

    assert lines[0].startswith("| Rank | Generator / Model | Pass | Safety |")
    # Header divider row for a GitHub-flavored table.
    assert set(lines[1].replace("|", "").split()) <= {":---", "---:", ":---:"}
    # Winner row: full safety earns a check, degraded safety earns a cross.
    assert "✅" in lines[2]
    assert "❌" in lines[3]
    assert lines[2].startswith("| 1 |")


def test_render_markdown_includes_warning_blockquote() -> None:
    reports = [
        _report(suite_name="smoke"),
        _report(suite_name="safety", model_name="sonnet"),
    ]

    md = render_markdown(build_leaderboard(reports, force=True))

    assert "> ⚠️" in md


def test_render_terminal_contains_header_and_columns() -> None:
    board = build_leaderboard([_report(model_name="opus")])

    text = render_terminal(board, color=False)

    assert "QueryPilot Eval Leaderboard" in text
    assert "Suite:   smoke (7 cases)" in text
    assert "Reports: 1" in text
    assert "Generator / Model" in text
    assert "Safety" in text
    assert "\033[" not in text


def test_render_terminal_color_wraps_safety_marker() -> None:
    board = build_leaderboard([_report(safety_pass_rate=1.0)])

    text = render_terminal(board, color=True)

    assert "\033[32m" in text  # green check for full safety


def test_render_terminal_case_counts_vary_label() -> None:
    reports = [_report(total_cases=7, passed=7), _report(total_cases=5, passed=5)]

    text = render_terminal(build_leaderboard(reports), color=False)

    assert "case counts vary" in text
    assert "Warnings" in text


def test_json_round_trip(tmp_path: Path) -> None:
    reports = [
        _report(model_name="opus", pass_rate=1.0),
        _report(model_name="haiku", pass_rate=0.78, estimated_cost_usd=3.6),
    ]
    board = build_leaderboard(reports)

    out = tmp_path / "leaderboard.json"
    write_json(board, out)
    reloaded = load_leaderboard(out)

    assert reloaded == board
    assert reloaded.entries[0].model_name == "opus"
    assert reloaded.entries[0].rank == 1


def test_write_markdown_creates_parent_dirs(tmp_path: Path) -> None:
    board = build_leaderboard([_report()])

    written = write_markdown(board, tmp_path / "nested" / "board.md")

    assert written.exists()
    assert written.read_text(encoding="utf-8").startswith("| Rank |")


def test_load_leaderboard_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_leaderboard(tmp_path / "missing.json")


def test_leaderboard_model_validates_json_shape() -> None:
    board = build_leaderboard([_report()])
    payload = board.model_dump_json()

    assert Leaderboard.model_validate_json(payload) == board
