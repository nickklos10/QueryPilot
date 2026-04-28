from __future__ import annotations

import time
import traceback
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from querypilot.audit import AuditMetadata, InMemoryAuditSink
from querypilot.core.client import QueryPilot
from querypilot.core.types import GeneratedSQL, ValidationResult
from querypilot.evals.compare import RowsetMatch, compare_rows
from querypilot.evals.cost import CostTracker, NullCostTracker, TokenUsage
from querypilot.evals.suite import BenchmarkCase, ComparisonConfig


class FailureCategory(str, Enum):
    GENERATION_FAILED = "generation_failed"
    VALIDATION_FAILED = "validation_failed"
    EXECUTION_FAILED = "execution_failed"
    RESULT_MISMATCH = "result_mismatch"
    SAFETY_FALSE_NEGATIVE = "safety_false_negative"
    SAFETY_FALSE_POSITIVE = "safety_false_positive"
    REPAIR_FAILED = "repair_failed"
    TIMEOUT = "timeout"
    COST_EXCEEDED = "cost_exceeded"
    LATENCY_EXCEEDED = "latency_exceeded"
    SCHEMA_SELECTION_FAILED = "schema_selection_failed"
    UNKNOWN_ERROR = "unknown_error"


class StageTimings(BaseModel):
    generate_ms: int = 0
    validate_ms: int = 0
    repair_ms: int = 0
    execute_ms: int = 0
    gold_execute_ms: int = 0
    compare_ms: int = 0
    total_ms: int = 0


class CaseResult(BaseModel):
    id: str
    passed: bool
    failure_category: FailureCategory | None = None

    question: str | None = None
    candidate_sql: str | None = None
    rewritten_sql: str | None = None
    gold_sql: str | None = None

    correctness_passed: bool = False
    safety_passed: bool = False
    validation_passed: bool = False
    execution_passed: bool = False

    repair_attempts: int = 0
    validation_errors: list[str] = Field(default_factory=list)
    rowset_match: RowsetMatch | None = None

    timings: StageTimings = Field(default_factory=StageTimings)
    token_usage: TokenUsage | None = None
    estimated_cost_usd: float | None = None

    tags: list[str] = Field(default_factory=list)
    error: str | None = None
    traceback: str | None = None


QueryPilotFactory = Callable[[BenchmarkCase], QueryPilot]


def run_case(
    case: BenchmarkCase,
    qp_factory: QueryPilotFactory,
    *,
    cost_tracker: CostTracker | None = None,
    comparison: ComparisonConfig | None = None,
) -> CaseResult:
    cost_tracker = cost_tracker or NullCostTracker()
    cost_tracker.reset()
    comparison = comparison or ComparisonConfig()
    overall_start = time.perf_counter()

    try:
        qp = qp_factory(case)
        qp = qp.with_audit_metadata(
            AuditMetadata(app_name="querypilot.eval", trace_id=case.id)
        )
        qp.audit_sink = InMemoryAuditSink()
        qp.generator = cost_tracker.wrap(qp.generator)

        if not case.should_pass and case.sql is not None:
            return _finalize(_run_safety_case(case, qp, cost_tracker), overall_start)

        return _finalize(
            _run_question_case(case, qp, cost_tracker, comparison), overall_start
        )
    except Exception as exc:
        return _finalize(
            CaseResult(
                id=case.id,
                passed=False,
                failure_category=FailureCategory.UNKNOWN_ERROR,
                tags=list(case.tags),
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            ),
            overall_start,
        )


def _finalize(result: CaseResult, overall_start: float) -> CaseResult:
    elapsed = int((time.perf_counter() - overall_start) * 1000)
    result.timings.total_ms = elapsed
    return result


def _run_safety_case(
    case: BenchmarkCase, qp: QueryPilot, cost_tracker: CostTracker
) -> CaseResult:
    timings = StageTimings()
    candidate_sql = case.sql or ""

    validation: ValidationResult
    with _timer() as t:
        validation = qp.validate_sql(candidate_sql)
    timings.validate_ms = t.elapsed_ms

    validation_blocked = not validation.valid
    safety_passed = validation_blocked
    expected_kind = case.expected_failure_kind or "validation"
    correct_kind = validation_blocked and expected_kind == "validation"

    error_text = "; ".join(validation.errors)
    error_match = _matches_expected_errors(error_text, case.expected_error_contains)

    passed = bool(safety_passed and correct_kind and error_match)
    failure_category: FailureCategory | None = None
    if not validation_blocked:
        failure_category = FailureCategory.SAFETY_FALSE_NEGATIVE
    elif not correct_kind:
        failure_category = FailureCategory.UNKNOWN_ERROR
    elif not error_match:
        failure_category = FailureCategory.UNKNOWN_ERROR

    return CaseResult(
        id=case.id,
        passed=passed,
        failure_category=failure_category,
        question=None,
        candidate_sql=candidate_sql,
        rewritten_sql=validation.rewritten_sql,
        gold_sql=case.gold_sql,
        correctness_passed=False,
        safety_passed=safety_passed,
        validation_passed=validation.valid,
        execution_passed=False,
        repair_attempts=0,
        validation_errors=list(validation.errors),
        timings=timings,
        token_usage=cost_tracker.last_usage(),
        estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
        tags=list(case.tags),
        error=None if passed else (error_text or "validator did not block unsafe SQL"),
    )


def _run_question_case(
    case: BenchmarkCase,
    qp: QueryPilot,
    cost_tracker: CostTracker,
    comparison: ComparisonConfig,
) -> CaseResult:
    timings = StageTimings()
    question = case.question or ""

    with _timer() as t:
        generated = qp.generate_sql(question)
    timings.generate_ms = t.elapsed_ms

    if generated.sql is None:
        return CaseResult(
            id=case.id,
            passed=False,
            failure_category=FailureCategory.GENERATION_FAILED,
            question=question,
            gold_sql=case.gold_sql,
            timings=timings,
            token_usage=cost_tracker.last_usage(),
            estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
            tags=list(case.tags),
            error="; ".join(generated.errors) or "generator returned no SQL",
        )

    validation, repair_attempts, validate_ms, repair_ms, final_sql = _validate_with_repair(
        qp, question, generated, case
    )
    timings.validate_ms = validate_ms
    timings.repair_ms = repair_ms

    if not validation.valid or validation.rewritten_sql is None:
        category = (
            FailureCategory.REPAIR_FAILED
            if repair_attempts > 0
            else FailureCategory.VALIDATION_FAILED
        )
        return CaseResult(
            id=case.id,
            passed=False,
            failure_category=category,
            question=question,
            candidate_sql=final_sql,
            rewritten_sql=validation.rewritten_sql,
            gold_sql=case.gold_sql,
            validation_passed=False,
            repair_attempts=repair_attempts,
            validation_errors=list(validation.errors),
            timings=timings,
            token_usage=cost_tracker.last_usage(),
            estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
            tags=list(case.tags),
            error="; ".join(validation.errors) or "validation failed",
        )

    schema_failure = _schema_selection_failure(case, validation)
    text_failure = _text_check_failure(case, validation.rewritten_sql or final_sql)

    candidate_rows: list[dict] | None = None
    execute_error: str | None = None
    try:
        with _timer() as t:
            result = qp.execute_sql(final_sql)
        timings.execute_ms = t.elapsed_ms
        candidate_rows = result.rows
    except Exception as exc:
        timings.execute_ms = 0
        execute_error = f"{type(exc).__name__}: {exc}"

    if candidate_rows is None:
        return CaseResult(
            id=case.id,
            passed=False,
            failure_category=FailureCategory.EXECUTION_FAILED,
            question=question,
            candidate_sql=final_sql,
            rewritten_sql=validation.rewritten_sql,
            gold_sql=case.gold_sql,
            validation_passed=True,
            execution_passed=False,
            repair_attempts=repair_attempts,
            validation_errors=list(validation.errors),
            timings=timings,
            token_usage=cost_tracker.last_usage(),
            estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
            tags=list(case.tags),
            error=execute_error,
        )

    if schema_failure is not None:
        return CaseResult(
            id=case.id,
            passed=False,
            failure_category=FailureCategory.SCHEMA_SELECTION_FAILED,
            question=question,
            candidate_sql=final_sql,
            rewritten_sql=validation.rewritten_sql,
            gold_sql=case.gold_sql,
            validation_passed=True,
            execution_passed=True,
            repair_attempts=repair_attempts,
            validation_errors=list(validation.errors),
            timings=timings,
            token_usage=cost_tracker.last_usage(),
            estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
            tags=list(case.tags),
            error=schema_failure,
        )

    if text_failure is not None:
        return CaseResult(
            id=case.id,
            passed=False,
            failure_category=FailureCategory.RESULT_MISMATCH,
            question=question,
            candidate_sql=final_sql,
            rewritten_sql=validation.rewritten_sql,
            gold_sql=case.gold_sql,
            validation_passed=True,
            execution_passed=True,
            repair_attempts=repair_attempts,
            validation_errors=list(validation.errors),
            timings=timings,
            token_usage=cost_tracker.last_usage(),
            estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
            tags=list(case.tags),
            error=text_failure,
        )

    rowset_match: RowsetMatch | None = None
    if case.gold_sql:
        fixture_db = case.fixture_db
        if fixture_db is None:
            return CaseResult(
                id=case.id,
                passed=False,
                failure_category=FailureCategory.UNKNOWN_ERROR,
                question=question,
                candidate_sql=final_sql,
                rewritten_sql=validation.rewritten_sql,
                gold_sql=case.gold_sql,
                tags=list(case.tags),
                error="case.fixture_db not resolved before run_case (qp_factory must set it).",
            )
        try:
            with _timer() as t:
                gold_rows = _execute_gold(fixture_db, case.gold_sql)
            timings.gold_execute_ms = t.elapsed_ms
        except Exception as exc:
            return CaseResult(
                id=case.id,
                passed=False,
                failure_category=FailureCategory.UNKNOWN_ERROR,
                question=question,
                candidate_sql=final_sql,
                rewritten_sql=validation.rewritten_sql,
                gold_sql=case.gold_sql,
                validation_passed=True,
                execution_passed=True,
                repair_attempts=repair_attempts,
                validation_errors=list(validation.errors),
                timings=timings,
                token_usage=cost_tracker.last_usage(),
                estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
                tags=list(case.tags),
                error=f"gold_sql execution failed: {exc}",
            )

        with _timer() as t:
            rowset_match = compare_rows(gold_rows, candidate_rows, case.gold_sql, comparison)
        timings.compare_ms = t.elapsed_ms

        if not rowset_match.matched:
            return CaseResult(
                id=case.id,
                passed=False,
                failure_category=FailureCategory.RESULT_MISMATCH,
                question=question,
                candidate_sql=final_sql,
                rewritten_sql=validation.rewritten_sql,
                gold_sql=case.gold_sql,
                correctness_passed=False,
                validation_passed=True,
                execution_passed=True,
                repair_attempts=repair_attempts,
                validation_errors=list(validation.errors),
                rowset_match=rowset_match,
                timings=timings,
                token_usage=cost_tracker.last_usage(),
                estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
                tags=list(case.tags),
                error="result rows did not match gold",
            )

    return CaseResult(
        id=case.id,
        passed=True,
        question=question,
        candidate_sql=generated.sql,
        rewritten_sql=validation.rewritten_sql,
        gold_sql=case.gold_sql,
        correctness_passed=case.gold_sql is not None,
        validation_passed=True,
        execution_passed=True,
        repair_attempts=repair_attempts,
        validation_errors=list(validation.errors),
        rowset_match=rowset_match,
        timings=timings,
        token_usage=cost_tracker.last_usage(),
        estimated_cost_usd=_estimated_cost(cost_tracker.last_usage()),
        tags=list(case.tags),
    )


def _validate_with_repair(
    qp: QueryPilot, question: str, generated: GeneratedSQL, case: BenchmarkCase
) -> tuple[ValidationResult, int, int, int, str]:
    validate_total = 0
    repair_total = 0
    repair_attempts = 0
    sql = generated.sql
    assert sql is not None

    with _timer() as t:
        validation = qp.validate_sql(sql)
    validate_total += t.elapsed_ms

    max_attempts = qp.config.max_generation_attempts
    while not (validation.valid and validation.rewritten_sql is not None):
        if not hasattr(qp.generator, "repair") or repair_attempts >= max_attempts - 1:
            break
        with _timer() as t:
            generated = qp.generator.repair(  # type: ignore[attr-defined]
                question,
                qp.get_schema(),
                qp.config.max_rows,
                sql,
                validation,
            )
        repair_total += t.elapsed_ms
        repair_attempts += 1
        if generated.sql is None:
            break
        sql = generated.sql
        with _timer() as t:
            validation = qp.validate_sql(sql)
        validate_total += t.elapsed_ms

    return validation, repair_attempts, validate_total, repair_total, sql


def _schema_selection_failure(case: BenchmarkCase, validation: ValidationResult) -> str | None:
    if not case.expected_tables:
        return None
    selected = {t.lower() for t in validation.tables}
    missing = [t for t in case.expected_tables if t.lower() not in selected]
    if missing:
        return f"expected tables not present in candidate: {', '.join(missing)}"
    return None


def _text_check_failure(case: BenchmarkCase, sql: str) -> str | None:
    lowered = sql.lower()
    for token in case.must_include:
        if token.lower() not in lowered:
            return f"candidate SQL missing required token: {token!r}"
    for token in case.must_not_contain:
        if token.lower() in lowered:
            return f"candidate SQL contained forbidden token: {token!r}"
    return None


def _matches_expected_errors(error_text: str, expected: list[str]) -> bool:
    if not expected:
        return True
    lowered = error_text.lower()
    return all(needle.lower() in lowered for needle in expected)


def _execute_gold(database_url: str, sql: str) -> list[dict]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            return [dict(row._mapping) for row in result.fetchall()]
    finally:
        engine.dispose()


def _estimated_cost(usage: TokenUsage | None) -> float | None:
    if usage is None:
        return None
    return usage.estimated_usd


class _Timer:
    def __init__(self) -> None:
        self.start = 0.0
        self.elapsed_ms = 0

    def __enter__(self) -> "_Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.elapsed_ms = int((time.perf_counter() - self.start) * 1000)


def _timer() -> _Timer:
    return _Timer()
