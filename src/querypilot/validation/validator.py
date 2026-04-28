from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import DatabaseSchema, ValidationResult
from querypilot.validation.policies import DANGEROUS_KEYWORDS


class SQLValidator:
    def __init__(self, config: QueryPilotConfig) -> None:
        self.config = config

    def validate(self, sql: str, schema: DatabaseSchema) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        readonly = True
        stripped = sql.strip().rstrip(";")

        if _contains_dangerous_keyword(stripped):
            readonly = False
            errors.append("SQL contains a blocked keyword.")

        try:
            expression = sqlglot.parse_one(stripped, read=self.config.dialect)
        except sqlglot.errors.SqlglotError as exc:
            return ValidationResult(
                valid=False,
                readonly=False,
                warnings=warnings,
                errors=[f"SQL parse error: {exc}"],
            )

        if not isinstance(expression, exp.Select):
            readonly = False
            errors.append("Only SELECT queries are allowed.")

        tables = sorted({_normalize_identifier(table.name) for table in expression.find_all(exp.Table)})
        columns = sorted(
            {
                _normalize_identifier(column.name)
                for column in expression.find_all(exp.Column)
                if column.name != "*"
            }
        )

        for table_name in tables:
            if schema.get_table(table_name) is None:
                errors.append(f"Unknown table: {table_name}")
            if self.config.blocked_tables and table_name.lower() in _lower_set(self.config.blocked_tables):
                errors.append(f"Blocked table referenced: {table_name}")
            if self.config.allowed_tables and table_name.lower() not in _lower_set(self.config.allowed_tables):
                errors.append(f"Table is not in allowed_tables: {table_name}")

        if len(tables) == 1:
            table = schema.get_table(tables[0])
            if table is not None:
                known_columns = {column.name.lower() for column in table.columns}
                for column_name in columns:
                    if column_name.lower() not in known_columns:
                        errors.append(f"Unknown column: {column_name}")
        elif len(tables) > 1:
            warnings.append("Column validation is limited for multi-table queries.")

        rewritten_sql, limit_applied = _apply_limit(expression, self.config.max_rows, self.config.dialect)

        return ValidationResult(
            valid=not errors,
            readonly=readonly and not errors,
            tables=tables,
            columns=columns,
            limit_applied=limit_applied,
            rewritten_sql=rewritten_sql if not errors else None,
            warnings=warnings,
            errors=errors,
        )


def _contains_dangerous_keyword(sql: str) -> bool:
    tokens = set(re.findall(r"[A-Za-z_]+", sql.upper()))
    return bool(tokens & DANGEROUS_KEYWORDS)


def _normalize_identifier(identifier: str) -> str:
    return identifier.strip('"').split(".")[-1]


def _lower_set(values: list[str]) -> set[str]:
    return {value.lower() for value in values}


def _apply_limit(expression: exp.Expression, max_rows: int, dialect: str) -> tuple[str, bool]:
    limit = expression.args.get("limit")
    if limit is None:
        expression = expression.copy()
        expression.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return expression.sql(dialect=dialect), True

    current_limit = _limit_value(limit)
    if current_limit is not None and current_limit > max_rows:
        expression = expression.copy()
        expression.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return expression.sql(dialect=dialect), True

    return expression.sql(dialect=dialect), False


def _limit_value(limit: exp.Expression) -> int | None:
    expression = limit.args.get("expression")
    if isinstance(expression, exp.Literal) and expression.is_number:
        return int(expression.this)
    return None
