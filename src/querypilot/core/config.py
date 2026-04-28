from __future__ import annotations

from pydantic import BaseModel, Field


class QueryPilotConfig(BaseModel):
    dialect: str = "sqlite"
    readonly: bool = True
    max_rows: int = Field(default=100, ge=1)
    timeout_seconds: int = Field(default=10, ge=1)
    allowed_tables: list[str] | None = None
    blocked_tables: list[str] | None = None
