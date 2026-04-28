from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

CaseSource = Literal["authored", "audit_replay"]
ExpectedFailureKind = Literal["validation", "generation", "execution"]


class ComparisonConfig(BaseModel):
    ignore_row_order: bool = True
    ignore_column_order: bool = True
    float_tolerance: float = 0.0
    normalize_datetimes: bool = True
    case_insensitive_strings: bool = False


class SuiteThresholds(BaseModel):
    pass_rate: float | None = None
    safety_pass_rate: float | None = None
    correctness_rate: float | None = None
    max_p95_latency_ms: int | None = None
    max_avg_cost_usd: float | None = None


class BenchmarkCase(BaseModel):
    id: str
    question: str | None = None
    sql: str | None = None
    gold_sql: str | None = None

    fixture_db: str | None = None
    fixture_dialect: str | None = None

    expected_tables: list[str] = Field(default_factory=list)
    expected_columns: list[str] = Field(default_factory=list)
    must_include: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)

    tags: list[str] = Field(default_factory=list)
    should_pass: bool = True
    expected_failure_kind: ExpectedFailureKind | None = None
    expected_error_contains: list[str] = Field(default_factory=list)
    notes: str | None = None

    source: CaseSource = "authored"
    audit_id: str | None = None
    original_timestamp: str | None = None
    original_question: str | None = None

    @model_validator(mode="after")
    def _check_inputs(self) -> "BenchmarkCase":
        if self.question is None and self.sql is None:
            raise ValueError(
                f"BenchmarkCase {self.id!r}: must set 'question' (NL case) or 'sql' (safety case)."
            )
        if not self.should_pass and self.expected_failure_kind is None:
            raise ValueError(
                f"BenchmarkCase {self.id!r}: should_pass=false requires expected_failure_kind "
                "('validation', 'generation', or 'execution')."
            )
        return self


class BenchmarkSuite(BaseModel):
    name: str
    fixture_db: str | None = None
    fixture_dialect: str = "sqlite"
    thresholds: SuiteThresholds = Field(default_factory=SuiteThresholds)
    comparison: ComparisonConfig = Field(default_factory=ComparisonConfig)
    cases: list[BenchmarkCase]

    @model_validator(mode="after")
    def _check_suite(self) -> "BenchmarkSuite":
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"Duplicate case id in suite {self.name!r}: {case.id!r}")
            seen.add(case.id)
            if case.fixture_db is None and self.fixture_db is None:
                raise ValueError(
                    f"BenchmarkCase {case.id!r}: fixture_db not set on case or suite."
                )
        return self

    def resolved_fixture_db(self, case: BenchmarkCase) -> str:
        return case.fixture_db or self.fixture_db or ""

    def resolved_fixture_dialect(self, case: BenchmarkCase) -> str:
        return case.fixture_dialect or self.fixture_dialect
