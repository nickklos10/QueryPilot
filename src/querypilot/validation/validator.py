from __future__ import annotations

import hashlib
import re

import sqlglot
from sqlglot import exp

from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import DatabaseSchema, PolicyCheck, ValidationResult
from querypilot.validation.policies import DANGEROUS_KEYWORDS


class SQLValidator:
    def __init__(self, config: QueryPilotConfig) -> None:
        self.config = config

    def validate(self, sql: str, schema: DatabaseSchema) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        checks: list[PolicyCheck] = []
        readonly = True
        stripped = sql.strip().rstrip(";")
        blocked_reason: str | None = None
        risk_level = "low"

        try:
            statements = sqlglot.parse(stripped, read=self.config.dialect)
        except sqlglot.errors.SqlglotError as exc:
            blocked_reason = f"SQL parse error: {exc}"
            checks.append(
                PolicyCheck(
                    name="parseable",
                    passed=False,
                    message=blocked_reason,
                    severity="critical",
                )
            )
            return ValidationResult(
                valid=False,
                readonly=False,
                risk_level="critical",
                blocked_reason=blocked_reason,
                policy_checks=checks,
                query_fingerprint=_fingerprint(stripped),
                warnings=warnings,
                errors=[blocked_reason],
            )

        if len(statements) != 1:
            readonly = False
            blocked_reason = "Multiple SQL statements are not allowed."
            errors.append(blocked_reason)
            checks.append(
                PolicyCheck(
                    name="single_statement",
                    passed=False,
                    message=blocked_reason,
                    severity="critical",
                )
            )
            return ValidationResult(
                valid=False,
                readonly=False,
                risk_level="critical",
                blocked_reason=blocked_reason,
                policy_checks=checks,
                query_fingerprint=_fingerprint(stripped),
                warnings=warnings,
                errors=errors,
            )

        checks.append(
            PolicyCheck(
                name="single_statement",
                passed=True,
                message="SQL contains one statement.",
            )
        )

        if _contains_dangerous_keyword(stripped):
            readonly = False
            errors.append("SQL contains a blocked keyword.")
            blocked_reason = blocked_reason or "SQL contains a blocked keyword."
            risk_level = _max_risk(risk_level, "critical")
            checks.append(
                PolicyCheck(
                    name="blocked_keywords",
                    passed=False,
                    message="Blocked keyword found.",
                    severity="critical",
                )
            )
        else:
            checks.append(
                PolicyCheck(
                    name="blocked_keywords",
                    passed=True,
                    message="No blocked keywords found.",
                )
            )

        expression = statements[0]
        checks.append(PolicyCheck(name="parseable", passed=True, message="SQL parsed successfully."))

        if not isinstance(expression, exp.Select):
            readonly = False
            errors.append("Only SELECT queries are allowed.")
            blocked_reason = blocked_reason or "Only SELECT queries are allowed."
            risk_level = _max_risk(risk_level, "critical")
            checks.append(
                PolicyCheck(
                    name="readonly_select",
                    passed=False,
                    message="Only SELECT queries are allowed.",
                    severity="critical",
                )
            )
        else:
            checks.append(
                PolicyCheck(
                    name="readonly_select",
                    passed=True,
                    message="Statement is read-only SELECT.",
                )
            )

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
                blocked_reason = blocked_reason or f"Unknown table: {table_name}"
            if self.config.blocked_tables and table_name.lower() in _lower_set(self.config.blocked_tables):
                errors.append(f"Blocked table referenced: {table_name}")
                blocked_reason = blocked_reason or f"Blocked table referenced: {table_name}"
            if self.config.allowed_tables and table_name.lower() not in _lower_set(self.config.allowed_tables):
                errors.append(f"Table is not in allowed_tables: {table_name}")
                blocked_reason = blocked_reason or f"Table is not in allowed_tables: {table_name}"

        table_policy_passed = not any(
            error.startswith(("Unknown table:", "Blocked table referenced:", "Table is not in allowed_tables:"))
            for error in errors
        )
        checks.append(
            PolicyCheck(
                name="known_tables",
                passed=table_policy_passed,
                message="Referenced tables are known and allowed."
                if table_policy_passed
                else "One or more referenced tables are unknown or disallowed.",
                severity="high" if not table_policy_passed else "low",
            )
        )

        if len(tables) == 1:
            table = schema.get_table(tables[0])
            if table is not None:
                known_columns = {column.name.lower() for column in table.columns}
                for column_name in columns:
                    if column_name.lower() not in known_columns:
                        errors.append(f"Unknown column: {column_name}")
                        blocked_reason = blocked_reason or f"Unknown column: {column_name}"
        elif len(tables) > 1:
            warnings.append("Column validation is limited for multi-table queries.")

        column_policy_passed = not any(error.startswith("Unknown column:") for error in errors)
        checks.append(
            PolicyCheck(
                name="known_columns",
                passed=column_policy_passed,
                message="Referenced columns are known where validation is feasible."
                if column_policy_passed
                else "One or more referenced columns are unknown.",
                severity="high" if not column_policy_passed else "low",
            )
        )

        has_select_star = any(isinstance(selected, exp.Star) for selected in expression.find_all(exp.Star))
        if has_select_star and self.config.safety_policy.warn_on_select_star:
            warnings.append("SELECT * may expose more data than intended.")
            risk_level = _max_risk(risk_level, "medium")
        if has_select_star and not self.config.safety_policy.allow_select_star:
            blocked_reason = blocked_reason or "SELECT * is disabled by policy."
            errors.append("SELECT * is disabled by policy.")
            checks.append(
                PolicyCheck(
                    name="select_star",
                    passed=False,
                    message="SELECT * is disabled by policy.",
                    severity="high",
                )
            )
            risk_level = _max_risk(risk_level, "high")
        else:
            checks.append(
                PolicyCheck(
                    name="select_star",
                    passed=True,
                    message="SELECT * policy satisfied."
                    if has_select_star
                    else "Query does not use SELECT *.",
                    severity="medium" if has_select_star else "low",
                )
            )

        if self.config.safety_policy.reject_cartesian_joins and _has_cartesian_join(expression):
            blocked_reason = blocked_reason or "Potential Cartesian join detected."
            errors.append("Potential Cartesian join detected.")
            checks.append(
                PolicyCheck(
                    name="join_safety",
                    passed=False,
                    message="Potential Cartesian join detected.",
                    severity="high",
                )
            )
            risk_level = _max_risk(risk_level, "high")
        else:
            checks.append(
                PolicyCheck(
                    name="join_safety",
                    passed=True,
                    message="No obvious Cartesian join detected.",
                )
            )

        rewritten_sql, limit_applied = _apply_limit(expression, self.config.max_rows, self.config.dialect)
        checks.append(
            PolicyCheck(
                name="row_limit",
                passed=True,
                message="Row limit is present and within policy.",
                severity="medium" if limit_applied else "low",
            )
        )

        if errors:
            risk_level = _max_risk(risk_level, _highest_error_risk(checks))

        return ValidationResult(
            valid=not errors,
            readonly=readonly and not errors,
            tables=tables,
            columns=columns,
            limit_applied=limit_applied,
            rewritten_sql=rewritten_sql if not errors else None,
            risk_level=risk_level,
            blocked_reason=blocked_reason,
            policy_checks=checks,
            query_fingerprint=_fingerprint(rewritten_sql if not errors else stripped),
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


def _has_cartesian_join(expression: exp.Expression) -> bool:
    if len(list(expression.find_all(exp.Table))) < 2:
        return False
    if expression.args.get("joins"):
        for join in expression.args["joins"]:
            if join.args.get("on") is None and join.args.get("using") is None:
                return True
        return False
    from_expression = expression.args.get("from")
    return from_expression is not None and len(list(from_expression.find_all(exp.Table))) > 1


def _fingerprint(sql: str) -> str:
    normalized = re.sub(r"\s+", " ", sql.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _max_risk(left: str, right: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return left if order[left] >= order[right] else right


def _highest_error_risk(checks: list[PolicyCheck]) -> str:
    risk = "low"
    for check in checks:
        if not check.passed:
            risk = _max_risk(risk, check.severity)
    return risk
