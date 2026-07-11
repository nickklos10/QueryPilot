from __future__ import annotations

from pydantic import BaseModel, Field

from querypilot.access import AccessPolicy
from querypilot.core.safety_defaults import BLOCKED_POSTGRES_FUNCTIONS


class SafetyPolicy(BaseModel):
    allow_select_star: bool = True
    reject_cartesian_joins: bool = True
    reject_multi_statement: bool = True
    warn_on_select_star: bool = True
    blocked_functions: list[str] = Field(
        default_factory=lambda: sorted(BLOCKED_POSTGRES_FUNCTIONS)
    )
    allowed_functions: list[str] | None = None


class QueryPilotConfig(BaseModel):
    dialect: str = "sqlite"
    readonly: bool = True
    max_rows: int = Field(default=100, ge=1)
    timeout_seconds: int = Field(default=10, ge=1)
    max_generation_attempts: int = Field(default=2, ge=1)
    allowed_tables: list[str] | None = None
    blocked_tables: list[str] | None = None
    safety_policy: SafetyPolicy = Field(default_factory=SafetyPolicy)
    access_policy: AccessPolicy = Field(default_factory=AccessPolicy)
