from __future__ import annotations

from pydantic import BaseModel, Field


class SafetyPolicy(BaseModel):
    allow_select_star: bool = True
    reject_cartesian_joins: bool = True
    reject_multi_statement: bool = True
    warn_on_select_star: bool = True


class QueryPilotConfig(BaseModel):
    dialect: str = "sqlite"
    readonly: bool = True
    max_rows: int = Field(default=100, ge=1)
    timeout_seconds: int = Field(default=10, ge=1)
    allowed_tables: list[str] | None = None
    blocked_tables: list[str] | None = None
    safety_policy: SafetyPolicy = Field(default_factory=SafetyPolicy)
