from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

from querypilot.access.policy import MASK_STRENGTH
from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import DatabaseSchema, PolicyCheck, ValidationResult

_QUERY_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_SET_OPERATION_TYPES = (exp.Union, exp.Intersect, exp.Except)
# sqlglot renamed ScopeType.UNION to SET_OPERATION; support both.
_SET_OPERATION_SCOPE = ScopeType["SET_OPERATION" if "SET_OPERATION" in ScopeType.__members__ else "UNION"]


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

        expression = statements[0]
        checks.append(PolicyCheck(name="parseable", passed=True, message="SQL parsed successfully."))

        is_query = isinstance(expression, _QUERY_TYPES)
        if not is_query:
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

        non_readonly_operation = _non_readonly_operation(expression)
        if isinstance(expression, exp.Select) and non_readonly_operation is not None:
            readonly = False
            operation_error = (
                f"SQL contains a non-read-only operation: {non_readonly_operation}"
            )
            errors.append(operation_error)
            blocked_reason = blocked_reason or operation_error
            risk_level = _max_risk(risk_level, "critical")
            checks.append(
                PolicyCheck(
                    name="statement_safety",
                    passed=False,
                    message=operation_error,
                    severity="critical",
                )
            )
        else:
            checks.append(
                PolicyCheck(
                    name="statement_safety",
                    passed=True,
                    message="No nested write or administrative operation found.",
                )
            )

        function_error = _function_policy_error(expression, self.config)
        if function_error is not None:
            errors.append(function_error)
            blocked_reason = blocked_reason or function_error
            risk_level = _max_risk(risk_level, "high")
            checks.append(
                PolicyCheck(
                    name="function_safety",
                    passed=False,
                    message=function_error,
                    severity="high",
                )
            )
        else:
            checks.append(
                PolicyCheck(
                    name="function_safety",
                    passed=True,
                    message="SQL functions satisfy policy.",
                )
            )

        source_error = _executable_comment_error(stripped, self.config.dialect) or (
            _external_source_error(expression)
        )
        if source_error is not None:
            readonly = False
            errors.append(source_error)
            blocked_reason = blocked_reason or source_error
            risk_level = _max_risk(risk_level, "critical")
            checks.append(
                PolicyCheck(
                    name="source_safety",
                    passed=False,
                    message=source_error,
                    severity="critical",
                )
            )
        else:
            checks.append(
                PolicyCheck(
                    name="source_safety",
                    passed=True,
                    message="No executable comments or file and URL table references.",
                )
            )

        analysis = _QueryAnalysis()
        if is_query and non_readonly_operation is None:
            try:
                analysis = _analyze_query(expression, schema)
            # sqlglot raises several error types on unusual trees; any failure fails closed.
            except Exception as exc:  # noqa: BLE001
                analysis = _QueryAnalysis(errors=[f"Could not analyze query structure: {exc}"])
        if analysis.errors:
            errors.extend(analysis.errors)
            blocked_reason = blocked_reason or analysis.errors[0]
            risk_level = _max_risk(risk_level, "high")

        tables = analysis.tables
        columns = sorted(
            {
                _normalize_identifier(column.name)
                for column in expression.find_all(exp.Column)
                if column.name != "*"
            }
        )
        access_policy_summary = self.config.access_policy.summary()

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

        column_refs, column_errors = analysis.column_refs, analysis.column_errors
        if column_errors:
            errors.extend(column_errors)
            blocked_reason = blocked_reason or column_errors[0]

        column_policy_passed = not column_errors
        checks.append(
            PolicyCheck(
                name="known_columns",
                passed=column_policy_passed,
                message="Referenced columns are known and unambiguous."
                if column_policy_passed
                else "One or more referenced columns are unknown or ambiguous.",
                severity="high" if not column_policy_passed else "low",
            )
        )

        access_errors = _access_policy_errors(
            column_refs,
            analysis.star_tables,
            schema,
            self.config.access_policy,
        )
        if access_errors:
            blocked_reason = blocked_reason or access_errors[0]
            errors.extend(access_errors)
            checks.append(
                PolicyCheck(
                    name="access_policy",
                    passed=False,
                    message="Access policy blocked one or more referenced columns.",
                    severity="high",
                )
            )
            risk_level = _max_risk(risk_level, "high")
            access_policy_summary["blocked_columns"] = _blocked_column_summary(access_errors)
        else:
            checks.append(
                PolicyCheck(
                    name="access_policy",
                    passed=True,
                    message="Column access policy satisfied.",
                )
            )

        has_select_star = analysis.has_star
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

        output_masks, mask_error = _output_masks(analysis, self.config.access_policy)
        if mask_error is not None:
            errors.append(mask_error)
            blocked_reason = blocked_reason or mask_error
            risk_level = _max_risk(risk_level, "high")

        expression, row_filter_errors = _apply_row_filters(
            expression,
            analysis,
            self.config.access_policy.row_filters,
            self.config.dialect,
        )
        if row_filter_errors:
            blocked_reason = blocked_reason or row_filter_errors[0]
            errors.extend(row_filter_errors)
            risk_level = _max_risk(risk_level, "high")
        access_policy_summary["row_filters"] = {
            table: filter_sql
            for table, filter_sql in self.config.access_policy.row_filters.items()
            if table in tables
        }

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
            access_policy=access_policy_summary,
            output_masks=output_masks if not errors else [],
            warnings=warnings,
            errors=errors,
        )


def _non_readonly_operation(expression: exp.Expression) -> str | None:
    blocked_types = (
        exp.Alter,
        exp.Command,
        exp.Copy,
        exp.Create,
        exp.Delete,
        exp.Drop,
        exp.Insert,
        exp.Into,
        exp.Merge,
        exp.TruncateTable,
        exp.Update,
    )
    for node in expression.walk():
        if node is expression:
            continue
        if isinstance(node, blocked_types):
            return type(node).__name__.upper()
    return None


def _function_policy_error(
    expression: exp.Expression,
    config: QueryPilotConfig,
) -> str | None:
    blocked = _lower_set(config.safety_policy.blocked_functions)
    allowed_values = config.safety_policy.allowed_functions
    allowed = _lower_set(allowed_values) if allowed_values is not None else None

    for function in expression.find_all(exp.Func):
        function_name = _function_name(function)
        if function_name in blocked:
            return f"SQL function is blocked by policy: {function_name}"
        if allowed is not None and function_name not in allowed:
            return f"SQL function is not allowed by policy: {function_name}"
    return None


def _executable_comment_error(sql: str, dialect: str) -> str | None:
    # MySQL runs the body of /*! ... */ (MariaDB also /*M! ... */) as SQL, so
    # anything hidden there escapes validation.
    for token in sqlglot.tokenize(sql, read=dialect):
        for comment in token.comments:
            if comment.startswith(("!", "M!")):
                return "Executable comments (/*! ... */) are not allowed."
    return None


def _external_source_error(expression: exp.Expression) -> str | None:
    # DuckDB reads `FROM 'data.csv'` or `FROM 's3://b/x.parquet'` as a file or URL
    # (a replacement scan), so a table name that looks like a path is not a table.
    for table in expression.find_all(exp.Table):
        for part in table.parts:
            if isinstance(part, exp.Identifier) and any(char in part.name for char in "./\\:"):
                return f"Table reference looks like a file path or URL: {part.name}"
    return None


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return function.name.lower()
    return function.sql_name().lower()


def _normalize_identifier(identifier: str) -> str:
    return identifier.strip('"').split(".")[-1]


@dataclass(frozen=True)
class _Output:
    name: str
    sources: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class _Resolution:
    sources: frozenset[tuple[str, str]] = frozenset()
    base_ref: tuple[str, str] | None = None
    error: str | None = None


@dataclass
class _QueryAnalysis:
    tables: list[str] = field(default_factory=list)
    column_refs: list[tuple[str, str]] = field(default_factory=list)
    column_errors: list[str] = field(default_factory=list)
    star_tables: set[str] = field(default_factory=set)
    has_star: bool = False
    # Every reference to a physical table, so row filters can wrap each one in place.
    base_tables: list[exp.Table] = field(default_factory=list)
    # Table nodes no scope claimed; row filters fail closed on these.
    unscoped_tables: list[exp.Table] = field(default_factory=list)
    outputs: list[_Output] = field(default_factory=list)
    outputs_complete: bool = True
    errors: list[str] = field(default_factory=list)


def _analyze_query(expression: exp.Expression, schema: DatabaseSchema) -> _QueryAnalysis:
    scopes = list(traverse_scope(expression))
    resolver = _ScopeResolver(scopes, schema)
    analysis = _QueryAnalysis()
    seen_tables: set[int] = set()
    column_errors: list[str] = []

    for scope in scopes:
        for table in scope.tables:
            seen_tables.add(id(table))
            if isinstance(scope.sources.get(table.alias_or_name), exp.Table):
                analysis.base_tables.append(table)

        if not isinstance(scope.expression, exp.Select):
            continue

        for column in _own_columns(scope):
            if _is_star(column):
                continue
            resolution = resolver.resolve(scope, column)
            if resolution.error is not None:
                column_errors.append(resolution.error)
            elif resolution.base_ref is not None:
                analysis.column_refs.append(resolution.base_ref)

        for projection in scope.expression.expressions:
            if not _is_star(projection):
                continue
            analysis.has_star = True
            for source in resolver.star_sources(scope, projection):
                if isinstance(source, exp.Table):
                    analysis.star_tables.add(_normalize_identifier(source.name).lower())

    analysis.unscoped_tables = [
        table for table in expression.find_all(exp.Table) if id(table) not in seen_tables
    ]
    analysis.tables = sorted(
        {
            _normalize_identifier(table.name).lower()
            for table in [*analysis.base_tables, *analysis.unscoped_tables]
        }
    )
    analysis.column_errors = list(dict.fromkeys(column_errors))

    root = resolver.scope_for(expression)
    analysis.outputs = resolver.outputs(root) if root is not None else []
    analysis.outputs_complete = root is not None and resolver.complete
    return analysis


class _ScopeResolver:
    """Resolves columns and output lineage through CTEs, derived tables, and set operations."""

    def __init__(self, scopes: list[Scope], schema: DatabaseSchema) -> None:
        self.schema = schema
        self.complete = True
        self._by_expression = {id(scope.expression): scope for scope in scopes}
        self._outputs: dict[int, list[_Output]] = {}

    def scope_for(self, node: exp.Expression) -> Scope | None:
        return self._by_expression.get(id(node))

    def resolve(self, scope: Scope, column: exp.Column) -> _Resolution:
        name = _normalize_identifier(column.name).lower()
        qualifier = _normalize_identifier(column.table).lower() if column.table else None

        for current in _lookup_chain(scope):
            sources = _selected_sources(current)
            if qualifier is not None:
                if qualifier in sources:
                    return self._resolve_in_source(sources[qualifier], name)
                continue
            matches = [source for source in sources.values() if self._has_column(source, name)]
            if len(matches) == 1:
                return self._resolve_in_source(matches[0], name)
            if len(matches) > 1:
                return _Resolution(error=f"Ambiguous column: {name}")
            if current is scope and _is_group_by_alias(scope, column, name):
                # Source columns win over select aliases in GROUP BY, so this is
                # reached only when no source has the name; the alias's own
                # expression is resolved with the projection.
                return _Resolution()

        if qualifier is not None:
            return _Resolution(error=f"Unknown table qualifier: {column.table}")
        return _Resolution(error=f"Unknown column: {name}")

    def star_sources(self, scope: Scope, star: exp.Expression) -> list[exp.Table | Scope]:
        sources = _selected_sources(scope)
        qualifier = star.table if isinstance(star, exp.Column) else ""
        if not qualifier:
            return list(sources.values())
        source = sources.get(_normalize_identifier(qualifier).lower())
        return [source] if source is not None else []

    def outputs(self, scope: Scope) -> list[_Output]:
        key = id(scope)
        if key in self._outputs:
            return self._outputs[key]
        self._outputs[key] = []

        expression = scope.expression
        if isinstance(expression, _SET_OPERATION_TYPES):
            # A recursive CTE's later branches reference this scope; the anchor
            # branch defines its columns, so expose those while the rest resolves.
            anchor = self._branch_outputs(expression.this)
            self._outputs[key] = _apply_alias_columns(expression, anchor)
            outputs = self._set_operation_outputs(expression)
        elif isinstance(expression, exp.Select):
            outputs = self._select_outputs(scope)
        else:
            self.complete = False
            outputs = []

        outputs = _apply_alias_columns(expression, outputs)
        self._outputs[key] = outputs
        return outputs

    def _resolve_in_source(self, source: exp.Table | Scope, name: str) -> _Resolution:
        if isinstance(source, exp.Table):
            table_name = _normalize_identifier(source.name).lower()
            table = self.schema.get_table(table_name)
            if table is not None and table.get_column(name) is None:
                return _Resolution(error=f"Unknown column: {name}")
            ref = (table_name, name)
            return _Resolution(sources=frozenset({ref}), base_ref=ref)

        matches = [output for output in self.outputs(source) if output.name == name]
        if not matches:
            return _Resolution(error=f"Unknown column: {name}")
        return _Resolution(sources=frozenset().union(*(output.sources for output in matches)))

    def _has_column(self, source: exp.Table | Scope, name: str) -> bool:
        if isinstance(source, exp.Table):
            table = self.schema.get_table(_normalize_identifier(source.name))
            return table is not None and table.get_column(name) is not None
        return any(output.name == name for output in self.outputs(source))

    def _select_outputs(self, scope: Scope) -> list[_Output]:
        own_columns = {id(column) for column in _own_columns(scope)}
        outputs: list[_Output] = []
        for projection in scope.expression.expressions:
            if _is_star(projection):
                for source in self.star_sources(scope, projection):
                    outputs.extend(self._source_outputs(source))
                continue

            sources: set[tuple[str, str]] = set()
            for column in projection.find_all(exp.Column):
                if id(column) not in own_columns or _is_star(column) or _inside_count(column, projection):
                    continue
                sources |= self.resolve(scope, column).sources
            for node in projection.find_all(*_QUERY_TYPES):
                child = self.scope_for(node)
                if child is not None:
                    for output in self.outputs(child):
                        sources |= output.sources
            outputs.append(
                _Output(
                    name=_normalize_identifier(projection.alias_or_name).lower(),
                    sources=frozenset(sources),
                )
            )
        return outputs

    def _set_operation_outputs(self, expression: exp.Expression) -> list[_Output]:
        left = self._branch_outputs(expression.this)
        right = self._branch_outputs(expression.expression)
        merged = [
            _Output(name=lhs.name, sources=lhs.sources | rhs.sources)
            for lhs, rhs in zip(left, right)
        ]
        return merged + left[len(right):]

    def _branch_outputs(self, node: exp.Expression) -> list[_Output]:
        if isinstance(node, exp.Subquery):
            node = node.this
        scope = self.scope_for(node)
        if scope is not None:
            return self.outputs(scope)
        if isinstance(node, _SET_OPERATION_TYPES):
            return self._set_operation_outputs(node)
        self.complete = False
        return []

    def _source_outputs(self, source: exp.Table | Scope) -> list[_Output]:
        if isinstance(source, Scope):
            return self.outputs(source)
        table_name = _normalize_identifier(source.name).lower()
        table = self.schema.get_table(table_name)
        if table is None:
            self.complete = False
            return []
        return [
            _Output(name=column.name.lower(), sources=frozenset({(table_name, column.name.lower())}))
            for column in table.columns
        ]


def _lookup_chain(scope: Scope):
    """Yield the scope, then enclosing scopes a correlated subquery may reference."""
    current: Scope | None = scope
    while current is not None:
        yield current
        if current.scope_type not in (ScopeType.SUBQUERY, _SET_OPERATION_SCOPE):
            return
        current = current.parent


def _own_columns(scope: Scope) -> list[exp.Column]:
    """Columns written in this scope's own query, not in a nested subquery.

    sqlglot also lists a child subquery's unqualified columns on the parent scope
    (as possible correlated references); those are resolved in the child scope.
    """
    return [
        column
        for column in scope.columns
        if column.find_ancestor(*_QUERY_TYPES) is scope.expression
    ]


def _is_group_by_alias(scope: Scope, column: exp.Column, name: str) -> bool:
    """Whether an unqualified GROUP BY column names one of this SELECT's aliases."""
    group = column.find_ancestor(exp.Group)
    if group is None or group.parent is not scope.expression:
        return False
    return any(
        isinstance(projection, exp.Alias)
        and _normalize_identifier(projection.alias).lower() == name
        for projection in scope.expression.expressions
    )


def _selected_sources(scope: Scope) -> dict[str, exp.Table | Scope]:
    return {
        _normalize_identifier(name).lower(): source
        for name, (_, source) in scope.selected_sources.items()
    }


def _apply_alias_columns(expression: exp.Expression, outputs: list[_Output]) -> list[_Output]:
    # The leftmost branch of a set operation names its columns, so climb to the
    # alias that owns it (sqlglot scopes a recursive CTE reference to its anchor).
    node = expression
    while isinstance(node.parent, _SET_OPERATION_TYPES) and node.parent.this is node:
        node = node.parent
    parent = node.parent
    if not isinstance(parent, (exp.Subquery, exp.CTE)):
        return outputs
    alias = parent.args.get("alias")
    names = [column.name.lower() for column in alias.columns] if alias is not None else []
    if not names:
        return outputs
    return [
        _Output(name=names[index] if index < len(names) else output.name, sources=output.sources)
        for index, output in enumerate(outputs)
    ]


def _is_star(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (
        isinstance(node, exp.Column) and isinstance(node.this, exp.Star)
    )


def _inside_count(column: exp.Expression, stop: exp.Expression) -> bool:
    node = column.parent
    while node is not None and node is not stop:
        if isinstance(node, exp.Count):
            return True
        node = node.parent
    return isinstance(stop, exp.Count)


def _output_masks(analysis: _QueryAnalysis, access_policy) -> tuple[list[str | None], str | None]:
    rules = {
        table.lower(): {column.lower(): rule.mode for column, rule in columns.items()}
        for table, columns in access_policy.masking_rules.items()
    }
    if not any(table in rules for table in analysis.tables):
        return [], None
    if not analysis.outputs_complete:
        return [], "Could not determine output columns for masking."

    masks: list[str | None] = []
    for output in analysis.outputs:
        modes = [rules[table][column] for table, column in output.sources if column in rules.get(table, {})]
        masks.append(min(modes, key=MASK_STRENGTH.__getitem__) if modes else None)
    return masks, None


def _lower_set(values: list[str]) -> set[str]:
    return {value.lower() for value in values}


def _apply_limit(expression: exp.Expression, max_rows: int, dialect: str) -> tuple[str, bool]:
    limit = expression.args.get("limit")
    current_limit = _limit_value(limit) if limit is not None else None
    # Only a plain non-negative integer literal within policy is kept; anything else
    # (LIMIT -1, expressions, subqueries, FETCH) is replaced so the cap cannot be evaded.
    if current_limit is not None and 0 <= current_limit <= max_rows:
        return expression.sql(dialect=dialect), False

    expression = expression.copy()
    expression.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return expression.sql(dialect=dialect), True


def _limit_value(limit: exp.Expression) -> int | None:
    if not isinstance(limit, exp.Limit):
        return None
    expression = limit.args.get("expression")
    if isinstance(expression, exp.Literal) and expression.is_number:
        try:
            return int(expression.this)
        except ValueError:
            return None
    return None


def _has_cartesian_join(expression: exp.Expression) -> bool:
    for select in expression.find_all(exp.Select):
        for join in select.args.get("joins") or []:
            if join.args.get("on") is None and join.args.get("using") is None:
                return True
    return False


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


def _access_policy_errors(
    column_refs: list[tuple[str, str]],
    star_tables: set[str],
    schema: DatabaseSchema,
    access_policy,
) -> list[str]:
    errors: list[str] = []
    blocked = _normalized_policy_map(access_policy.blocked_columns)
    allowed = _normalized_policy_map(access_policy.allowed_columns)

    for table_name, column_name in column_refs:
        blocked_columns = blocked.get(table_name, set())
        if column_name in blocked_columns:
            errors.append(f"Column is blocked by access policy: {table_name}.{column_name}")
            continue

        allowed_columns = allowed.get(table_name)
        if allowed_columns is not None and column_name not in allowed_columns:
            errors.append(f"Column is not allowed by access policy: {table_name}.{column_name}")

    for table_name in sorted(star_tables):
        blocked_columns = blocked.get(table_name, set())
        if blocked_columns:
            column_name = min(blocked_columns)
            errors.append(f"Column is blocked by access policy: {table_name}.{column_name}")

        allowed_columns = allowed.get(table_name)
        table = schema.get_table(table_name)
        if allowed_columns is not None and table is not None:
            exposed_columns = {column.name.lower() for column in table.columns}
            disallowed_columns = sorted(exposed_columns - allowed_columns)
            if disallowed_columns:
                errors.append(
                    "Column is not allowed by access policy: "
                    f"{table_name}.{disallowed_columns[0]}"
                )

    return list(dict.fromkeys(errors))


def _normalized_policy_map(policy: dict[str, list[str]]) -> dict[str, set[str]]:
    return {
        table.lower(): {column.lower() for column in columns}
        for table, columns in policy.items()
    }


def _blocked_column_summary(errors: list[str]) -> list[str]:
    blocked: list[str] = []
    for error in errors:
        if ": " in error:
            blocked.append(error.split(": ", 1)[1])
    return sorted(blocked)


def _apply_row_filters(
    expression: exp.Expression,
    analysis: _QueryAnalysis,
    row_filters: dict[str, str],
    dialect: str,
) -> tuple[exp.Expression, list[str]]:
    """Replace every reference to a filtered table with a filtered derived table.

    Filtering the relation itself (rather than appending to the outer WHERE) applies
    the filter inside subqueries, CTEs, and set operations, cannot bind to a column
    of another table or a spoofed alias, and keeps outer-join semantics intact.
    """
    filters = {table.lower(): filter_sql for table, filter_sql in row_filters.items() if filter_sql}
    if not filters:
        return expression, []

    errors: list[str] = []
    for table in analysis.unscoped_tables:
        table_name = _normalize_identifier(table.name).lower()
        if table_name in filters:
            errors.append(f"Row filter for {table_name} could not be applied to every reference.")

    conditions: dict[str, exp.Expression | None] = {}
    for table in analysis.base_tables:
        table_name = _normalize_identifier(table.name).lower()
        if table_name not in filters:
            continue
        if table_name not in conditions:
            try:
                conditions[table_name] = sqlglot.parse_one(
                    filters[table_name], into=exp.Condition, read=dialect
                )
            except sqlglot.errors.SqlglotError as exc:
                errors.append(f"Invalid row filter for {table_name}: {exc}")
                conditions[table_name] = None
        condition = conditions[table_name]
        if condition is not None:
            table.replace(_filtered_relation(table, condition))
    return expression, errors


def _filtered_relation(table: exp.Table, condition: exp.Expression) -> exp.Subquery:
    alias = table.args.get("alias")
    alias = alias.copy() if alias is not None else exp.TableAlias(this=table.this.copy())
    inner = table.copy()
    inner.set("alias", None)
    query = exp.select("*").from_(inner).where(condition.copy())
    return exp.Subquery(this=query, alias=alias)
