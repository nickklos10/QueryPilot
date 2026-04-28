from __future__ import annotations

from pydantic import BaseModel, Field

from querypilot.core.types import ValidationResult


class EvalCase(BaseModel):
    name: str
    question: str | None = None
    sql: str | None = None
    expected_tables: list[str] = Field(default_factory=list)
    expected_sql_contains: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    should_pass: bool = True


class EvalResult(BaseModel):
    name: str
    passed: bool
    generated_sql: str | None = None
    validation: ValidationResult | None = None
    errors: list[str] = Field(default_factory=list)


class EvalReport(BaseModel):
    total: int
    passed: int
    failed: int
    results: list[EvalResult] = Field(default_factory=list)
