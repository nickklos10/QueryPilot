from __future__ import annotations

from pathlib import Path

from querypilot.evals.suite_runner import SuiteReport


_CHECK = "✅"  # ✅
_CROSS = "❌"  # ❌


def write_json(report: SuiteReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return target


def render_terminal(report: SuiteReport, *, color: bool = True) -> str:
    use_color = color
    lines: list[str] = []
    lines.extend(_header_lines(report))
    lines.append("")
    lines.extend(_overall_lines(report, use_color=use_color))
    lines.append("")
    lines.extend(_tag_lines(report))
    lines.append("")
    lines.extend(_failure_breakdown_lines(report))
    lines.append("")
    lines.extend(_repair_summary_lines(report))
    lines.append("")
    lines.extend(_latency_cost_lines(report))
    lines.append("")
    lines.extend(_violations_footer(report, use_color=use_color))
    return "\n".join(lines)


def _header_lines(report: SuiteReport) -> list[str]:
    duration_seconds = report.duration_ms / 1000.0
    started = report.started_at.strftime("%Y-%m-%d %H:%M:%SZ")
    generator = report.generator_name or "n/a"
    if report.model_name:
        generator = f"{generator} ({report.model_name})"

    return [
        "QueryPilot Eval Report",
        f"Suite:     {report.suite_name}",
        f"Generator: {generator}",
        f"Database:  {report.database_url or 'n/a'}",
        f"Started:   {started}",
        f"Duration:  {duration_seconds:.1f}s",
    ]


def _overall_lines(report: SuiteReport, *, use_color: bool) -> list[str]:
    safety_total = sum(
        1 for r in report.case_results if _safety(r)
    )
    safety_passed = sum(
        1 for r in report.case_results if _safety(r) and r.safety_passed
    )
    correctness_total = sum(1 for r in report.case_results if r.gold_sql is not None)
    correctness_passed = sum(
        1 for r in report.case_results if r.gold_sql is not None and r.correctness_passed
    )
    repaired = sum(1 for r in report.case_results if r.repair_attempts > 0)
    repaired_passed = sum(
        1 for r in report.case_results if r.repair_attempts > 0 and r.passed
    )

    rows = [
        (
            "Pass rate",
            f"{report.passed} / {report.total_cases}",
            f"({_pct(report.pass_rate)})",
            _check_or_cross(
                report.pass_rate,
                report.thresholds.pass_rate,
                use_color=use_color,
            ),
        ),
        (
            "Safety pass rate",
            f"{safety_passed} / {safety_total}",
            f"({_pct(report.safety_pass_rate)})",
            _check_or_cross(
                report.safety_pass_rate,
                report.thresholds.safety_pass_rate,
                use_color=use_color,
            ),
        ),
        (
            "Correctness",
            f"{correctness_passed} / {correctness_total}",
            f"({_pct(report.correctness_rate)})",
            _check_or_cross(
                report.correctness_rate,
                report.thresholds.correctness_rate,
                use_color=use_color,
            ),
        ),
        (
            "Repair success",
            f"{repaired_passed} / {repaired} repaired",
            "",
            _maybe_check(use_color=use_color, value=True),
        ),
        (
            "Avg latency",
            f"{report.avg_latency_ms} ms",
            "",
            _maybe_check(use_color=use_color, value=True),
        ),
        (
            "P95 latency",
            f"{report.p95_latency_ms} ms",
            "",
            _check_below_max(
                report.p95_latency_ms,
                report.thresholds.max_p95_latency_ms,
                use_color=use_color,
            ),
        ),
        (
            "Estimated cost",
            f"${report.estimated_cost_usd:.2f}",
            "",
            _maybe_check(use_color=use_color, value=True),
        ),
    ]

    out = ["Overall"]
    for label, value, percent, marker in rows:
        out.append(_format_row(marker, label, value, percent))
    return out


def _tag_lines(report: SuiteReport) -> list[str]:
    if not report.tag_rollups:
        return ["Tag rollups", "  (none)"]
    out = ["Tag rollups"]
    width = max(len(t) for t in report.tag_rollups)
    for tag in sorted(report.tag_rollups):
        roll = report.tag_rollups[tag]
        out.append(
            f"  {tag.ljust(width)}    {roll.passed} / {roll.total} passed "
            f"({_pct(roll.pass_rate)})"
        )
    return out


def _failure_breakdown_lines(report: SuiteReport) -> list[str]:
    out = ["Failure breakdown"]
    if not report.failure_breakdown:
        out.append("  (none)")
        return out
    width = max(len(k) for k in report.failure_breakdown)
    for category in sorted(report.failure_breakdown):
        count = report.failure_breakdown[category]
        out.append(f"  {category.ljust(width)}    {count}")
    return out


def _repair_summary_lines(report: SuiteReport) -> list[str]:
    return [
        "Repair summary",
        f"  First-pass success    {_pct(report.first_pass_rate)}",
        f"  Final pass rate       {_pct(report.pass_rate)}",
        f"  Repair rate           {_pct(report.repair_rate)}",
        f"  Avg repair attempts   {report.avg_repair_attempts:.2f}",
    ]


def _latency_cost_lines(report: SuiteReport) -> list[str]:
    repair_label = "(when triggered)"
    return [
        "Latency & cost",
        f"  generate              {report.avg_generate_ms} ms avg",
        f"  validate              {report.avg_validate_ms} ms avg",
        f"  repair                {report.avg_repair_ms_when_triggered} ms avg {repair_label}",
        f"  execute               {report.avg_execute_ms} ms avg",
        f"  total tokens          prompt={report.total_prompt_tokens}, "
        f"completion={report.total_completion_tokens}",
        f"  estimated cost        ${report.estimated_cost_usd:.4f}",
    ]


def _violations_footer(report: SuiteReport, *, use_color: bool) -> list[str]:
    if not report.threshold_violations:
        return [_color(f"{_CHECK} No threshold violations.", "green", use_color)]
    lines = [_color(f"{_CROSS} Threshold violations:", "red", use_color)]
    for v in report.threshold_violations:
        lines.append(f"  - {v}")
    return lines


def _format_row(marker: str, label: str, value: str, percent: str) -> str:
    label_padded = label.ljust(22)
    value_padded = value.rjust(15)
    if percent:
        percent = " " + percent
    return f"  {marker}  {label_padded}{value_padded}{percent}"


def _safety(result) -> bool:  # pragma: no cover - trivial passthrough
    from querypilot.evals.pipeline import FailureCategory

    if result.failure_category in (
        FailureCategory.SAFETY_FALSE_NEGATIVE,
        FailureCategory.SAFETY_FALSE_POSITIVE,
    ):
        return True
    return result.safety_passed


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _check_or_cross(actual: float, threshold: float | None, *, use_color: bool) -> str:
    if threshold is None:
        return _maybe_check(use_color=use_color, value=True)
    return _maybe_check(use_color=use_color, value=actual >= threshold)


def _check_below_max(value: int, threshold: int | None, *, use_color: bool) -> str:
    if threshold is None:
        return _maybe_check(use_color=use_color, value=True)
    return _maybe_check(use_color=use_color, value=value <= threshold)


def _maybe_check(*, use_color: bool, value: bool) -> str:
    if value:
        return _color(_CHECK, "green", use_color)
    return _color(_CROSS, "red", use_color)


def _color(text: str, kind: str, use_color: bool) -> str:
    if not use_color:
        return text
    codes = {"green": "\033[32m", "red": "\033[31m"}
    reset = "\033[0m"
    code = codes.get(kind, "")
    if not code:
        return text
    return f"{code}{text}{reset}"
