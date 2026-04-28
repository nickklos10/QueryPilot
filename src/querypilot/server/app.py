from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from querypilot import QueryPilot
from querypilot.core.config import SafetyPolicy
from querypilot.evals.cases import EvalCase
from querypilot.evals.runner import run_eval_cases


class QuestionRequest(BaseModel):
    question: str


class SQLRequest(BaseModel):
    sql: str


class SchemaSearchRequest(BaseModel):
    query: str


class EvalRunRequest(BaseModel):
    cases: list[EvalCase] = Field(default_factory=list)


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
        return [match.model_dump() for match in qp.search_schema(request.query)]

    @app.post("/ask")
    def ask(request: QuestionRequest) -> dict[str, Any]:
        try:
            return qp.ask(request.question).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/generate-sql")
    def generate_sql(request: QuestionRequest) -> dict[str, Any]:
        return qp.generate_sql(request.question).model_dump()

    @app.post("/validate-sql")
    def validate_sql(request: SQLRequest) -> dict[str, Any]:
        return qp.validate_sql(request.sql).model_dump()

    @app.post("/execute-sql")
    def execute_sql(request: SQLRequest) -> dict[str, Any]:
        try:
            return qp.execute_sql(request.sql).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/evals/run")
    def run_evals(request: EvalRunRequest) -> dict[str, Any]:
        return run_eval_cases(qp, request.cases).model_dump()

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
    )
