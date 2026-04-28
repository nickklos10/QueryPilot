from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from querypilot.evals.suite_runner import SuiteReport


class CaseRegression(BaseModel):
    id: str
    was_passing: bool
    now_passing: bool
    previous_failure_category: str | None = None
    current_failure_category: str | None = None


class CheckSummary(BaseModel):
    pass_rate: float
    safety_pass_rate: float
    correctness_rate: float
    p95_latency_ms: int
    estimated_cost_usd: float
    total_cases: int
    passed: int
    failed: int


class CheckOutcome(BaseModel):
    ok: bool
    reasons: list[str] = Field(default_factory=list)
    current: CheckSummary
    baseline: CheckSummary | None = None
    regressed_cases: list[CaseRegression] = Field(default_factory=list)
    latency_p95_delta_ms: int | None = None


def check_report(
    report: SuiteReport,
    *,
    baseline: SuiteReport | None = None,
    threshold: float | None = None,
    max_p95_ms: int | None = None,
    require_safety_pass_rate: float | None = None,
    require_correctness_rate: float | None = None,
) -> CheckOutcome:
    reasons: list[str] = []

    if threshold is not None and report.pass_rate < threshold:
        reasons.append(
            f"pass_rate {report.pass_rate:.3f} below threshold {threshold:.3f}"
        )

    if (
        require_safety_pass_rate is not None
        and report.safety_pass_rate < require_safety_pass_rate
    ):
        reasons.append(
            f"safety_pass_rate {report.safety_pass_rate:.3f} below required "
            f"{require_safety_pass_rate:.3f}"
        )

    if (
        require_correctness_rate is not None
        and report.correctness_rate < require_correctness_rate
    ):
        reasons.append(
            f"correctness_rate {report.correctness_rate:.3f} below required "
            f"{require_correctness_rate:.3f}"
        )

    if max_p95_ms is not None and report.p95_latency_ms > max_p95_ms:
        reasons.append(
            f"p95_latency_ms {report.p95_latency_ms} exceeds {max_p95_ms}"
        )

    for violation in report.threshold_violations:
        if violation not in reasons:
            reasons.append(violation)

    regressed: list[CaseRegression] = []
    latency_delta: int | None = None
    baseline_summary: CheckSummary | None = None

    if baseline is not None:
        baseline_summary = _summary(baseline)
        latency_delta = report.p95_latency_ms - baseline.p95_latency_ms

        if report.pass_rate < baseline.pass_rate:
            reasons.append(
                f"pass_rate dropped from baseline {baseline.pass_rate:.3f} to "
                f"{report.pass_rate:.3f}"
            )

        if report.safety_pass_rate < baseline.safety_pass_rate:
            reasons.append(
                f"safety_pass_rate dropped from baseline "
                f"{baseline.safety_pass_rate:.3f} to {report.safety_pass_rate:.3f}"
            )

        if report.correctness_rate < baseline.correctness_rate:
            reasons.append(
                f"correctness_rate dropped from baseline "
                f"{baseline.correctness_rate:.3f} to {report.correctness_rate:.3f}"
            )

        regressed = _diff_cases(baseline=baseline, current=report)

    return CheckOutcome(
        ok=not reasons and not regressed,
        reasons=reasons,
        current=_summary(report),
        baseline=baseline_summary,
        regressed_cases=regressed,
        latency_p95_delta_ms=latency_delta,
    )


def load_report(path: str | Path) -> SuiteReport:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Report file not found: {target}")
    return SuiteReport.model_validate_json(target.read_text(encoding="utf-8"))


def format_outcome(outcome: CheckOutcome) -> str:
    if outcome.ok:
        return f"OK\n  pass_rate {outcome.current.pass_rate:.3f}, p95 {outcome.current.p95_latency_ms} ms"

    lines = ["Regression detected.", ""]
    if outcome.baseline is not None:
        lines.extend(
            [
                "Pass rate:",
                f"  baseline: {outcome.baseline.pass_rate * 100:.0f}%",
                f"  current:  {outcome.current.pass_rate * 100:.0f}%",
                "",
            ]
        )

    if outcome.regressed_cases:
        lines.append("Failed cases (regression vs. baseline):")
        for case in outcome.regressed_cases:
            verb = "was passing" if case.was_passing else "was failing"
            now = "now passing" if case.now_passing else (
                f"now {case.current_failure_category}" if case.current_failure_category
                else "now failing"
            )
            lines.append(f"  - {case.id} ({verb} -> {now})")
        lines.append("")

    if outcome.latency_p95_delta_ms is not None and outcome.baseline is not None:
        lines.extend(
            [
                "Latency:",
                f"  baseline p95: {outcome.baseline.p95_latency_ms} ms",
                f"  current p95:  {outcome.current.p95_latency_ms} ms"
                + (f"  ({outcome.latency_p95_delta_ms:+d} ms)" if outcome.latency_p95_delta_ms else ""),
                "",
            ]
        )

    if outcome.reasons:
        lines.append("Reasons:")
        for r in outcome.reasons:
            lines.append(f"  - {r}")

    return "\n".join(lines).rstrip() + "\n"


def write_outcome(outcome: CheckOutcome, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(outcome.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _summary(report: SuiteReport) -> CheckSummary:
    return CheckSummary(
        pass_rate=report.pass_rate,
        safety_pass_rate=report.safety_pass_rate,
        correctness_rate=report.correctness_rate,
        p95_latency_ms=report.p95_latency_ms,
        estimated_cost_usd=report.estimated_cost_usd,
        total_cases=report.total_cases,
        passed=report.passed,
        failed=report.failed,
    )


def _diff_cases(*, baseline: SuiteReport, current: SuiteReport) -> list[CaseRegression]:
    baseline_by_id = {c.id: c for c in baseline.case_results}
    current_by_id = {c.id: c for c in current.case_results}

    out: list[CaseRegression] = []
    for case_id, current_case in current_by_id.items():
        baseline_case = baseline_by_id.get(case_id)
        if baseline_case is None:
            continue
        if baseline_case.passed and not current_case.passed:
            out.append(
                CaseRegression(
                    id=case_id,
                    was_passing=True,
                    now_passing=False,
                    previous_failure_category=(
                        baseline_case.failure_category.value
                        if baseline_case.failure_category
                        else None
                    ),
                    current_failure_category=(
                        current_case.failure_category.value
                        if current_case.failure_category
                        else None
                    ),
                )
            )
    return out
