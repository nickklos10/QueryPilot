from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MaskingRule(BaseModel):
    mode: Literal["redact", "null", "hash"] = "redact"


class AccessPolicy(BaseModel):
    allowed_columns: dict[str, list[str]] = Field(default_factory=dict)
    blocked_columns: dict[str, list[str]] = Field(default_factory=dict)
    row_filters: dict[str, str] = Field(default_factory=dict)
    masking_rules: dict[str, dict[str, MaskingRule]] = Field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "allowed_columns": {
                table: sorted(columns) for table, columns in self.allowed_columns.items()
            },
            "blocked_columns": {
                table: sorted(columns) for table, columns in self.blocked_columns.items()
            },
            "row_filters": dict(self.row_filters),
            "masked_columns": {
                f"{table}.{column}": rule.mode
                for table, rules in self.masking_rules.items()
                for column, rule in rules.items()
            },
        }
