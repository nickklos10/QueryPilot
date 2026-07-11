from __future__ import annotations

import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Callable

from pydantic import BaseModel, Field

from querypilot.evals.cost import CostTracker, NullCostTracker
from querypilot.evals.pipeline import (
    CaseResult,
    FailureCategory,
    QueryPilotFactory,
    run_case,
)
from querypilot.evals.suite import (
    BenchmarkCase,
    BenchmarkSuite,
    SuiteThresholds,
)


class TagRollup(BaseModel):
    tag: str
    total: int
    passed: int
    pass_rate: float


class SuiteReport(BaseModel):
    suite_name: str
    generator_name: str | None = None
    model_name: str | None = None
    database_url: str | None = None
    querypilot_version: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int

    total_cases: int
    passed: int
    failed: int
    pass_rate: float
    safety_pass_rate: float
    correctness_rate: float
    repair_rate: float
    first_pass_rate: float
    avg_repair_attempts: float

    avg_latency_ms: int
    p50_latency_ms: int
    p95_latency_ms: int

    avg_generate_ms: int
    avg_validate_ms: int
    avg_repair_ms_when_triggered: int
    avg_execute_ms: int

    total_prompt_tokens: int
    total_completion_tokens: int
    estimated_cost_usd: float

    tag_rollups: dict[str, TagRollup] = Field(default_factory=dict)
    failure_breakdown: dict[str, int] = Field(default_factory=dict)
    thresholds: SuiteThresholds
    threshold_violations: list[str] = Field(default_factory=list)

    case_results: list[CaseResult]


CostTrackerFactory = Callable[[], CostTracker]


def run_suite(
    suite: BenchmarkSuite,
    *,
    qp_factory: QueryPilotFactory,
    cost_tracker_factory: CostTrackerFactory = NullCostTracker,
    max_workers: int = 1,
    generator_name: str | None = None,
    model_name: str | None = None,
    database_url: str | None = None,
) -> SuiteReport:
    started_at = datetime.now(UTC)
    started = _now_seconds()

    materialized = [_materialize_case(suite, case) for case in suite.cases]

    if max_workers <= 1:
        results = [
            run_case(
                case,
                qp_factory,
                cost_tracker=cost_tracker_factory(),
                comparison=suite.comparison,
            )
            for case in materialized
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(
                pool.map(
                    lambda case: run_case(
                        case,
                        qp_factory,
                        cost_tracker=cost_tracker_factory(),
                        comparison=suite.comparison,
                    ),
                    materialized,
                )
            )

    finished_at = datetime.now(UTC)
    duration_ms = int((_now_seconds() - started) * 1000)

    return _build_report(
        suite=suite,
        results=results,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        generator_name=generator_name,
        model_name=model_name,
        database_url=database_url,
    )


def _build_report(
    *,
    suite: BenchmarkSuite,
    results: list[CaseResult],
    started_at: datetime,
    finished_at: datetime,
    duration_ms: int,
    generator_name: str | None,
    model_name: str | None,
    database_url: str | None,
) -> SuiteReport:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    pass_rate = passed / total if total else 0.0

    safety_total = sum(1 for r in results if _is_safety_case(r))
    safety_passed = sum(1 for r in results if _is_safety_case(r) and r.safety_passed)
    safety_pass_rate = safety_passed / safety_total if safety_total else 1.0

    correctness_total = sum(1 for r in results if r.gold_sql is not None)
    correctness_passed = sum(
        1 for r in results if r.gold_sql is not None and r.correctness_passed
    )
    correctness_rate = correctness_passed / correctness_total if correctness_total else 1.0

    repaired = sum(1 for r in results if r.repair_attempts > 0)
    repair_rate = repaired / total if total else 0.0
    first_pass = sum(1 for r in results if r.passed and r.repair_attempts == 0)
    first_pass_rate = first_pass / total if total else 0.0
    avg_repair_attempts = (
        statistics.fmean(r.repair_attempts for r in results) if results else 0.0
    )

    latencies = [r.timings.total_ms for r in results]
    avg_latency_ms = int(statistics.fmean(latencies)) if latencies else 0
    p50_latency_ms = int(_percentile(latencies, 50)) if latencies else 0
    p95_latency_ms = int(_percentile(latencies, 95)) if latencies else 0

    avg_generate_ms = _avg_field(results, "generate_ms")
    avg_validate_ms = _avg_field(results, "validate_ms")
    avg_execute_ms = _avg_field(results, "execute_ms")
    repaired_results = [r for r in results if r.repair_attempts > 0]
    avg_repair_ms_when_triggered = (
        int(statistics.fmean(r.timings.repair_ms for r in repaired_results))
        if repaired_results
        else 0
    )

    total_prompt = sum(r.token_usage.prompt_tokens for r in results if r.token_usage)
    total_completion = sum(
        r.token_usage.completion_tokens for r in results if r.token_usage
    )
    estimated_cost = round(
        sum(r.estimated_cost_usd for r in results if r.estimated_cost_usd is not None),
        6,
    )

    tag_rollups = _build_tag_rollups(results)
    failure_breakdown = _build_failure_breakdown(results)
    threshold_violations = _check_thresholds(
        thresholds=suite.thresholds,
        pass_rate=pass_rate,
        safety_pass_rate=safety_pass_rate,
        correctness_rate=correctness_rate,
        p95_latency_ms=p95_latency_ms,
        avg_cost_usd=(estimated_cost / total) if total else 0.0,
    )

    return SuiteReport(
        suite_name=suite.name,
        generator_name=generator_name,
        model_name=model_name,
        database_url=database_url or suite.fixture_db,
        querypilot_version=_querypilot_version(),
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        total_cases=total,
        passed=passed,
        failed=failed,
        pass_rate=pass_rate,
        safety_pass_rate=safety_pass_rate,
        correctness_rate=correctness_rate,
        repair_rate=repair_rate,
        first_pass_rate=first_pass_rate,
        avg_repair_attempts=round(avg_repair_attempts, 3),
        avg_latency_ms=avg_latency_ms,
        p50_latency_ms=p50_latency_ms,
        p95_latency_ms=p95_latency_ms,
        avg_generate_ms=avg_generate_ms,
        avg_validate_ms=avg_validate_ms,
        avg_repair_ms_when_triggered=avg_repair_ms_when_triggered,
        avg_execute_ms=avg_execute_ms,
        total_prompt_tokens=total_prompt,
        total_completion_tokens=total_completion,
        estimated_cost_usd=estimated_cost,
        tag_rollups=tag_rollups,
        failure_breakdown=failure_breakdown,
        thresholds=suite.thresholds,
        threshold_violations=threshold_violations,
        case_results=results,
    )


def _materialize_case(suite: BenchmarkSuite, case: BenchmarkCase) -> BenchmarkCase:
    if case.fixture_db is not None and case.fixture_dialect is not None:
        return case
    return case.model_copy(
        update={
            "fixture_db": suite.resolved_fixture_db(case) or case.fixture_db,
            "fixture_dialect": suite.resolved_fixture_dialect(case),
        }
    )


def _is_safety_case(result: CaseResult) -> bool:
    if result.failure_category in (
        FailureCategory.SAFETY_FALSE_NEGATIVE,
        FailureCategory.SAFETY_FALSE_POSITIVE,
    ):
        return True
    return result.safety_passed


def _avg_field(results: list[CaseResult], field: str) -> int:
    if not results:
        return 0
    values = [getattr(r.timings, field) for r in results]
    return int(statistics.fmean(values))


def _percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (pct / 100.0) * (len(sorted_values) - 1)
    lower = int(k)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = k - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _build_tag_rollups(results: list[CaseResult]) -> dict[str, TagRollup]:
    totals: dict[str, int] = {}
    passes: dict[str, int] = {}
    for r in results:
        for tag in r.tags:
            totals[tag] = totals.get(tag, 0) + 1
            if r.passed:
                passes[tag] = passes.get(tag, 0) + 1
    rollups: dict[str, TagRollup] = {}
    for tag, total in totals.items():
        passed = passes.get(tag, 0)
        rollups[tag] = TagRollup(
            tag=tag,
            total=total,
            passed=passed,
            pass_rate=passed / total if total else 0.0,
        )
    return rollups


def _build_failure_breakdown(results: list[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        if r.failure_category is None:
            continue
        key = r.failure_category.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _check_thresholds(
    *,
    thresholds: SuiteThresholds,
    pass_rate: float,
    safety_pass_rate: float,
    correctness_rate: float,
    p95_latency_ms: int,
    avg_cost_usd: float,
) -> list[str]:
    violations: list[str] = []
    if thresholds.pass_rate is not None and pass_rate < thresholds.pass_rate:
        violations.append(
            f"pass_rate {pass_rate:.3f} below threshold {thresholds.pass_rate:.3f}"
        )
    if (
        thresholds.safety_pass_rate is not None
        and safety_pass_rate < thresholds.safety_pass_rate
    ):
        violations.append(
            f"safety_pass_rate {safety_pass_rate:.3f} below threshold "
            f"{thresholds.safety_pass_rate:.3f}"
        )
    if (
        thresholds.correctness_rate is not None
        and correctness_rate < thresholds.correctness_rate
    ):
        violations.append(
            f"correctness_rate {correctness_rate:.3f} below threshold "
            f"{thresholds.correctness_rate:.3f}"
        )
    if (
        thresholds.max_p95_latency_ms is not None
        and p95_latency_ms > thresholds.max_p95_latency_ms
    ):
        violations.append(
            f"p95_latency_ms {p95_latency_ms} exceeds threshold "
            f"{thresholds.max_p95_latency_ms}"
        )
    if thresholds.max_avg_cost_usd is not None and avg_cost_usd > thresholds.max_avg_cost_usd:
        violations.append(
            f"avg_cost_usd {avg_cost_usd:.6f} exceeds threshold "
            f"{thresholds.max_avg_cost_usd:.6f}"
        )
    return violations


def _querypilot_version() -> str:
    try:
        return version("querypilot")
    except PackageNotFoundError:
        return "0.0.0"


def _now_seconds() -> float:
    import time

    return time.perf_counter()
