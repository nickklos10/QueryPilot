from __future__ import annotations

from pydantic import BaseModel, Field

from querypilot.access import AccessPolicy
from querypilot.core.safety_defaults import DEFAULT_BLOCKED_FUNCTIONS


class SafetyPolicy(BaseModel):
    allow_select_star: bool = True
    reject_cartesian_joins: bool = True
    reject_multi_statement: bool = True
    warn_on_select_star: bool = True
    blocked_functions: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_BLOCKED_FUNCTIONS)
    )
    allowed_functions: list[str] | None = None


class QueryPilotConfig(BaseModel):
    dialect: str = "sqlite"
    readonly: bool = True
    max_rows: int = Field(default=100, ge=1)
    timeout_seconds: int = Field(default=10, ge=1)
    max_generation_attempts: int = Field(default=3, ge=1)
    allowed_tables: list[str] | None = None
    blocked_tables: list[str] | None = None
    # Database schemas (namespaces) to introspect; None means the connection's default.
    schemas: list[str] | None = None
    # Business rules and glossary entries passed to the model with the schema,
    # e.g. "Revenue means SUM(amount_cents) / 100 over paid invoices".
    hints: list[str] = Field(default_factory=list)
    # Descriptions keyed by "table" or "table.column"; they override database comments.
    descriptions: dict[str, str] = Field(default_factory=dict)
    # Distinct values sampled per text column for the prompt; 0 disables sampling.
    sample_values: int = Field(default=3, ge=0)
    # Above roughly this many prompt tokens of schema, only relevant tables are sent.
    schema_token_budget: int = Field(default=6000, ge=0)
    safety_policy: SafetyPolicy = Field(default_factory=SafetyPolicy)
    access_policy: AccessPolicy = Field(default_factory=AccessPolicy)
