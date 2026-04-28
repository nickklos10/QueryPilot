from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from querypilot import QueryPilot
from querypilot.access import AccessPolicy
from querypilot.audit import AuditMetadata
from querypilot.core.config import SafetyPolicy
from querypilot.evals.pipeline import run_case
from querypilot.evals.suite import BenchmarkCase, ComparisonConfig


class QuestionRequest(BaseModel):
    question: str
    metadata: AuditMetadata | None = None


class SQLRequest(BaseModel):
    sql: str
    metadata: AuditMetadata | None = None


class SchemaSearchRequest(BaseModel):
    query: str
    metadata: AuditMetadata | None = None


class EvalRunRequest(BaseModel):
    cases: list[BenchmarkCase] = Field(default_factory=list)
    comparison: ComparisonConfig | None = None


def create_app(
    querypilot: QueryPilot | None = None,
    *,
    database_url: str | None = None,
    dialect: str = "sqlite",
    readonly: bool = True,
    max_rows: int = 100,
    timeout_seconds: int = 10,
    max_generation_attempts: int = 2,
    allowed_tables: list[str] | None = None,
    blocked_tables: list[str] | None = None,
    safety_policy: SafetyPolicy | None = None,
    access_policy: AccessPolicy | None = None,
) -> FastAPI:
    qp = querypilot or _connect_querypilot(
        database_url=database_url,
        dialect=dialect,
        readonly=readonly,
        max_rows=max_rows,
        timeout_seconds=timeout_seconds,
        max_generation_attempts=max_generation_attempts,
        allowed_tables=allowed_tables,
        blocked_tables=blocked_tables,
        safety_policy=safety_policy,
        access_policy=access_policy,
    )

    app = FastAPI(
        title="QueryPilot",
        description="Safe SQL tool layer for AI agents.",
        version="0.1.0",
    )
    app.state.querypilot = qp

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/schema")
    def get_schema() -> dict[str, Any]:
        return qp.get_schema().model_dump()

    @app.post("/search-schema")
    def search_schema(request: SchemaSearchRequest) -> list[dict[str, Any]]:
        scoped_qp = qp.with_audit_metadata(request.metadata)
        return [match.model_dump() for match in scoped_qp.search_schema(request.query)]

    @app.post("/ask")
    def ask(request: QuestionRequest) -> dict[str, Any]:
        scoped_qp = qp.with_audit_metadata(request.metadata)
        try:
            return scoped_qp.ask(request.question).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/generate-sql")
    def generate_sql(request: QuestionRequest) -> dict[str, Any]:
        scoped_qp = qp.with_audit_metadata(request.metadata)
        return scoped_qp.generate_sql(request.question).model_dump()

    @app.post("/validate-sql")
    def validate_sql(request: SQLRequest) -> dict[str, Any]:
        scoped_qp = qp.with_audit_metadata(request.metadata)
        return scoped_qp.validate_sql(request.sql).model_dump()

    @app.post("/execute-sql")
    def execute_sql(request: SQLRequest) -> dict[str, Any]:
        scoped_qp = qp.with_audit_metadata(request.metadata)
        try:
            return scoped_qp.execute_sql(request.sql).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/evals/run")
    def run_evals(request: EvalRunRequest) -> dict[str, Any]:
        comparison = request.comparison or ComparisonConfig()
        results = [
            run_case(case, lambda _: qp, comparison=comparison).model_dump(mode="json")
            for case in request.cases
        ]
        passed = sum(1 for r in results if r["passed"])
        return {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "case_results": results,
        }

    @app.get("/audit/recent")
    def recent_audit(limit: int = 100) -> list[dict[str, Any]]:
        return [record.model_dump() for record in qp.get_audit_records(limit)]

    return app


def _connect_querypilot(
    *,
    database_url: str | None,
    dialect: str,
    readonly: bool,
    max_rows: int,
    timeout_seconds: int,
    max_generation_attempts: int,
    allowed_tables: list[str] | None,
    blocked_tables: list[str] | None,
    safety_policy: SafetyPolicy | None,
    access_policy: AccessPolicy | None,
) -> QueryPilot:
    if database_url is None:
        raise ValueError("database_url is required when querypilot is not provided.")
    return QueryPilot.connect(
        database_url=database_url,
        dialect=dialect,
        readonly=readonly,
        max_rows=max_rows,
        timeout_seconds=timeout_seconds,
        max_generation_attempts=max_generation_attempts,
        allowed_tables=allowed_tables,
        blocked_tables=blocked_tables,
        safety_policy=safety_policy,
        access_policy=access_policy,
    )
