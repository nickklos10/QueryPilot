from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from querypilot.core.types import ValidationResult


class AuditMetadata(BaseModel):
    actor: str | None = None
    session_id: str | None = None
    app_name: str | None = None
    trace_id: str | None = None


class QueryAuditRecord(BaseModel):
    audit_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    operation: str
    question: str | None = None
    sql: str | None = None
    rewritten_sql: str | None = None
    validation: ValidationResult | None = None
    valid: bool | None = None
    executed: bool = False
    row_count: int | None = None
    execution_time_ms: int | None = None
    error: str | None = None
    actor: str | None = None
    session_id: str | None = None
    app_name: str | None = None
    trace_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        operation: str,
        metadata: AuditMetadata | None = None,
        **kwargs,
    ) -> "QueryAuditRecord":
        metadata = metadata or AuditMetadata()
        return cls(
            operation=operation,
            actor=metadata.actor,
            session_id=metadata.session_id,
            app_name=metadata.app_name,
            trace_id=metadata.trace_id,
            **kwargs,
        )
