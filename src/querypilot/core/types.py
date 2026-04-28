from __future__ import annotations

from pydantic import BaseModel, Field


class ColumnSchema(BaseModel):
    name: str
    type: str
    nullable: bool = True


class TableSchema(BaseModel):
    name: str
    columns: list[ColumnSchema] = Field(default_factory=list)

    def get_column(self, name: str) -> ColumnSchema | None:
        lowered = name.lower()
        return next((column for column in self.columns if column.name.lower() == lowered), None)


class DatabaseSchema(BaseModel):
    dialect: str
    tables: list[TableSchema] = Field(default_factory=list)

    def get_table(self, name: str) -> TableSchema | None:
        normalized = name.split(".")[-1].strip('"').lower()
        return next((table for table in self.tables if table.name.lower() == normalized), None)

    @property
    def table_names(self) -> set[str]:
        return {table.name.lower() for table in self.tables}


class SchemaMatch(BaseModel):
    table: str
    column: str | None = None
    score: int
    reason: str


class GeneratedSQL(BaseModel):
    question: str
    sql: str | None
    explanation: str | None = None
    errors: list[str] = Field(default_factory=list)


class PolicyCheck(BaseModel):
    name: str
    passed: bool
    message: str
    severity: str = "low"


class ValidationResult(BaseModel):
    valid: bool
    readonly: bool
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    limit_applied: bool = False
    rewritten_sql: str | None = None
    risk_level: str = "low"
    blocked_reason: str | None = None
    policy_checks: list[PolicyCheck] = Field(default_factory=list)
    query_fingerprint: str | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class QueryResult(BaseModel):
    sql: str
    rows: list[dict] = Field(default_factory=list)
    row_count: int
    execution_time_ms: int


class QueryPilotAnswer(BaseModel):
    question: str
    sql: str
    rows: list[dict] = Field(default_factory=list)
    explanation: str
    validation: ValidationResult
    execution_time_ms: int
