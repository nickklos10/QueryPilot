from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from querypilot.evals.suite_runner import SuiteReport

_CHECK = "✅"
_CROSS = "❌"

# A generator/model is judged "safe" only when it never emitted destructive or
# out-of-policy SQL across the suite (safety_pass_rate == 1.0).
_SAFE_EPSILON = 1e-9


class LeaderboardError(ValueError):
    """Raised when a set of reports cannot be aggregated into a leaderboard."""


class LeaderboardEntry(BaseModel):
    rank: int
    label: str
    generator_name: str | None = None
    model_name: str | None = None
    suite_name: str

    total_cases: int
    passed: int
    pass_rate: float
    safety_pass_rate: float
    correctness_rate: float
    repair_rate: float
    first_pass_rate: float

    p50_latency_ms: int
    p95_latency_ms: int
    estimated_cost_usd: float

    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int


class Leaderboard(BaseModel):
    suite_name: str
    report_count: int
    entries: list[LeaderboardEntry]
    warnings: list[str] = Field(default_factory=list)


def default_label(report: SuiteReport) -> str:
    """Human label for a report, mirroring the ``report.py`` header format."""
    generator = report.generator_name or "n/a"
    if report.model_name:
        return f"{generator} ({report.model_name})"
    return generator


def build_leaderboard(
    reports: list[SuiteReport],
    *,
    labels: list[str] | None = None,
    force: bool = False,
) -> Leaderboard:
    """Aggregate N ``SuiteReport`` objects into a ranked leaderboard.

    Reports are ranked by pass rate (desc), tie-broken by safety pass rate
    (desc) then estimated cost (asc). Reports that disagree on suite name or
    case count raise a warning; mixing genuinely different suites is refused
    unless ``force`` is set.
    """
    if not reports:
        raise LeaderboardError("At least one report is required.")

    if labels is not None and len(labels) != len(reports):
        raise LeaderboardError(
            f"--labels count ({len(labels)}) does not match report count "
            f"({len(reports)})."
        )

    warnings: list[str] = []

    suite_names = sorted({r.suite_name for r in reports})
    if len(suite_names) > 1:
        detail = ", ".join(suite_names)
        if not force:
            raise LeaderboardError(
                f"Reports span multiple suites ({detail}). Refusing to mix "
                f"different suites; pass force=True to override."
            )
        warnings.append(f"Reports span multiple suites: {detail}.")

    case_counts = sorted({r.total_cases for r in reports})
    if len(case_counts) > 1:
        counts = ", ".join(str(c) for c in case_counts)
        warnings.append(
            f"Reports disagree on case count ({counts}); rates may not be "
            f"directly comparable."
        )

    entries: list[LeaderboardEntry] = []
    for index, report in enumerate(reports):
        label = labels[index] if labels is not None else default_label(report)
        entries.append(
            LeaderboardEntry(
                rank=0,
                label=label,
                generator_name=report.generator_name,
                model_name=report.model_name,
                suite_name=report.suite_name,
                total_cases=report.total_cases,
                passed=report.passed,
                pass_rate=report.pass_rate,
                safety_pass_rate=report.safety_pass_rate,
                correctness_rate=report.correctness_rate,
                repair_rate=report.repair_rate,
                first_pass_rate=report.first_pass_rate,
                p50_latency_ms=report.p50_latency_ms,
                p95_latency_ms=report.p95_latency_ms,
                estimated_cost_usd=report.estimated_cost_usd,
                total_prompt_tokens=report.total_prompt_tokens,
                total_completion_tokens=report.total_completion_tokens,
                total_tokens=(
                    report.total_prompt_tokens + report.total_completion_tokens
                ),
            )
        )

    entries.sort(
        key=lambda e: (-e.pass_rate, -e.safety_pass_rate, e.estimated_cost_usd)
    )
    for rank, entry in enumerate(entries, start=1):
        entry.rank = rank

    suite_name = suite_names[0] if len(suite_names) == 1 else "(mixed)"
    return Leaderboard(
        suite_name=suite_name,
        report_count=len(reports),
        entries=entries,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

_COLUMNS: list[tuple[str, str]] = [
    ("Rank", "right"),
    ("Generator / Model", "left"),
    ("Pass", "right"),
    ("Safety", "left"),
    ("Correct", "right"),
    ("Repair", "right"),
    ("p50/p95 ms", "right"),
    ("Cost", "right"),
    ("Tokens", "right"),
]


def render_terminal(board: Leaderboard, *, color: bool = True) -> str:
    """Render the leaderboard as an aligned terminal table."""
    lines: list[str] = ["QueryPilot Eval Leaderboard"]
    lines.append(f"Suite:   {board.suite_name} ({_case_summary(board)})")
    lines.append(f"Reports: {board.report_count}")
    lines.append("")

    rows = [_row_cells(entry) for entry in board.entries]
    widths = [len(header) for header, _ in _COLUMNS]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    header = _format_row([h for h, _ in _COLUMNS], widths)
    lines.append(header)
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(_colorize(_format_row(row, widths), use_color=color))

    if board.warnings:
        lines.append("")
        lines.append("Warnings")
        for warning in board.warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines)


def render_markdown(board: Leaderboard) -> str:
    """Render a clean GitHub-flavored markdown table for a blog post."""
    headers = [h for h, _ in _COLUMNS]
    aligns = {
        "left": ":---",
        "right": "---:",
        "center": ":---:",
    }
    # The Safety column carries an emoji marker, so center it.
    divider = []
    for header, align in _COLUMNS:
        divider.append(aligns["center"] if header == "Safety" else aligns[align])

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(divider) + " |",
    ]
    for entry in board.entries:
        lines.append("| " + " | ".join(_row_cells(entry)) + " |")

    if board.warnings:
        lines.append("")
        for warning in board.warnings:
            lines.append(f"> ⚠️ {warning}")

    return "\n".join(lines) + "\n"


def write_markdown(board: Leaderboard, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(board), encoding="utf-8")
    return target


def write_json(board: Leaderboard, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(board.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return target


def load_leaderboard(path: str | Path) -> Leaderboard:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Leaderboard file not found: {target}")
    return Leaderboard.model_validate_json(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def _row_cells(entry: LeaderboardEntry) -> list[str]:
    return [
        str(entry.rank),
        entry.label,
        _pct(entry.pass_rate),
        _safety_cell(entry.safety_pass_rate),
        _pct(entry.correctness_rate),
        _pct(entry.repair_rate),
        f"{entry.p50_latency_ms}/{entry.p95_latency_ms}",
        _cost(entry.estimated_cost_usd),
        _tokens(entry.total_tokens),
    ]


def _format_row(cells: list[str], widths: list[int]) -> str:
    parts: list[str] = []
    for cell, width, (_, align) in zip(cells, widths, _COLUMNS):
        if align == "left":
            parts.append(cell.ljust(width))
        else:
            parts.append(cell.rjust(width))
    return "  ".join(parts).rstrip()


def _case_summary(board: Leaderboard) -> str:
    counts = {entry.total_cases for entry in board.entries}
    if len(counts) == 1:
        return f"{next(iter(counts))} cases"
    return "case counts vary"


def _safety_cell(rate: float) -> str:
    marker = _CHECK if rate >= 1.0 - _SAFE_EPSILON else _CROSS
    return f"{_pct(rate)} {marker}"


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _cost(usd: float) -> str:
    if 0 < usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:,.2f}"


def _tokens(count: int) -> str:
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _colorize(text: str, *, use_color: bool) -> str:
    if not use_color:
        return text
    text = text.replace(_CHECK, f"\033[32m{_CHECK}\033[0m")
    text = text.replace(_CROSS, f"\033[31m{_CROSS}\033[0m")
    return text
