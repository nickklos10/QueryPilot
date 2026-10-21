"""Decide what the proxy does with one SQL statement.

Every statement lands in exactly one route:

- session commands the drivers need (BEGIN/COMMIT, SET and SHOW of an allowlisted
  setting, DEALLOCATE) are re-generated from their tokens, never forwarded verbatim;
- catalog queries that read only ``pg_catalog`` / ``information_schema`` with
  allowlisted functions are forwarded unchanged (no user data, no rewrite);
- everything else goes through ``SQLValidator`` and only its rewritten SQL is sent.

Anything that matches none of these is rejected, so a new client feature fails
closed instead of reaching the database unchecked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.tokens import Token, TokenType

from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import DatabaseSchema, ValidationResult
from querypilot.validation.feedback import public_errors
from querypilot.validation.validator import SQLValidator

# Settings a client may change; each is session formatting, none affects what data
# a query can reach. search_path is left out on purpose: it would let a client point
# an unqualified table name at another schema's table of the same name.
SETTABLE = frozenset(
    {
        "application_name",
        "bytea_output",
        "client_encoding",
        "client_min_messages",
        "datestyle",
        "extra_float_digits",
        "intervalstyle",
        "standard_conforming_strings",
        "timezone",
    }
)
# Values outside these sets would change how the proxy must parse or encode text.
_FIXED_VALUES = {
    "client_encoding": {"utf8", "utf-8", "unicode"},
    "standard_conforming_strings": {"on", "true"},
}
SHOWABLE = SETTABLE | {
    "default_transaction_read_only",
    "integer_datetimes",
    "is_superuser",
    "lc_collate",
    "lc_ctype",
    "max_identifier_length",
    "search_path",
    "server_encoding",
    "server_version",
    "server_version_num",
    "statement_timeout",
    "transaction_isolation",
    "transaction_read_only",
}
_SETTING_ALIASES = {
    "time zone": "timezone",
    "transaction isolation level": "transaction_isolation",
}
_ISOLATION_LEVELS = ("SERIALIZABLE", "REPEATABLE READ", "READ COMMITTED", "READ UNCOMMITTED")

# Catalog relations that hold row data or other sessions' query text.
_DENIED_CATALOG_PREFIXES = ("pg_stat_", "pg_statio_", "pg_stats", "pg_largeobject")
_DENIED_CATALOG_RELATIONS = frozenset(
    {
        "pg_authid",
        "pg_file_settings",
        "pg_hba_file_rules",
        "pg_ident_file_mappings",
        "pg_shadow",
        "pg_statistic",
        "pg_statistic_ext_data",
        "pg_user_mapping",
        "pg_user_mappings",
    }
)
_CATALOG_SCHEMAS = frozenset({"pg_catalog", "information_schema"})

# Postgres functions catalog queries may call (unqualified or pg_catalog-qualified).
# Functions sqlglot knows as standard SQL (COALESCE, LOWER, COUNT, ...) are allowed
# unless the safety policy blocks them; anything else, including any function that
# takes a query string, is rejected.
CATALOG_FUNCTIONS = frozenset(
    {
        "array_agg",
        "array_length",
        "array_lower",
        "array_position",
        "array_to_string",
        "array_upper",
        "col_description",
        "current_database",
        "current_schema",
        "current_schemas",
        "current_setting",
        "current_user",
        "format_type",
        "generate_subscripts",
        "has_column_privilege",
        "has_database_privilege",
        "has_function_privilege",
        "has_schema_privilege",
        "has_table_privilege",
        "json_agg",
        "json_build_object",
        "jsonb_agg",
        "jsonb_build_object",
        "obj_description",
        "pg_backend_pid",
        "pg_collation_is_visible",
        "pg_encoding_to_char",
        "pg_function_is_visible",
        "pg_get_constraintdef",
        "pg_get_expr",
        "pg_get_function_arguments",
        "pg_get_function_identity_arguments",
        "pg_get_function_result",
        "pg_get_indexdef",
        "pg_get_keywords",
        "pg_get_partkeydef",
        "pg_get_partition_constraintdef",
        "pg_get_ruledef",
        "pg_get_serial_sequence",
        "pg_get_statisticsobjdef_columns",
        "pg_get_triggerdef",
        "pg_get_userbyid",
        "pg_get_viewdef",
        "pg_has_role",
        "pg_indexes_size",
        "pg_is_in_recovery",
        "pg_opclass_is_visible",
        "pg_partition_ancestors",
        "pg_partition_root",
        "pg_partition_tree",
        "pg_operator_is_visible",
        "pg_relation_is_publishable",
        "pg_relation_size",
        "pg_size_pretty",
        "pg_table_is_visible",
        "pg_table_size",
        "pg_total_relation_size",
        "pg_ts_config_is_visible",
        "pg_type_is_visible",
        "quote_ident",
        "session_user",
        "string_agg",
        "to_regclass",
        "to_regnamespace",
        "to_regproc",
        "to_regprocedure",
        "to_regrole",
        "to_regtype",
        "unnest",
        "version",
    }
)


@dataclass
class Statement:
    """One classified statement.

    ``kind`` is one of: ``empty``, ``begin``, ``commit``, ``rollback``,
    ``savepoint``, ``set``, ``show``, ``deallocate``, ``catalog``, ``query``,
    ``rejected``. ``upstream_sql`` is the only text ever sent to the database.
    """

    kind: str
    sql: str
    upstream_sql: str | None = None
    # Command tag for statements answered by the proxy itself.
    tag: str | None = None
    validation: ValidationResult | None = None
    output_masks: list[str | None] = field(default_factory=list)
    # Rejections: SQLSTATE, agent-facing message, and the full errors for the audit log.
    sqlstate: str | None = None
    message: str | None = None
    errors: list[str] = field(default_factory=list)
    # DEALLOCATE target; None means ALL.
    target: str | None = None

    @property
    def returns_rows(self) -> bool:
        return self.kind in ("catalog", "query", "show")


class StatementPolicy:
    """Routes statements for one proxy; shared by every client session."""

    def __init__(
        self,
        config: QueryPilotConfig,
        schema: DatabaseSchema,
        *,
        catalog_relations: set[str],
        data_schemas: set[str],
        allow_catalog: bool = True,
    ) -> None:
        self.config = config
        self.schema = schema
        self.validator = SQLValidator(config)
        self.catalog_relations = {name.lower() for name in catalog_relations}
        self.data_schemas = {name.lower() for name in data_schemas}
        self.allow_catalog = allow_catalog

    def classify(self, sql: str) -> Statement:
        try:
            tokens = sqlglot.tokenize(sql, read="postgres")
        except sqlglot.errors.SqlglotError as exc:
            return _reject(sql, "42601", f"SQL parse error: {exc}")
        tokens = [token for token in tokens if token.token_type != TokenType.SEMICOLON]
        if not tokens:
            return Statement(kind="empty", sql=sql)

        command = _session_command(sql, tokens)
        if command is not None:
            return command

        try:
            expression = sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.SqlglotError as exc:
            return _reject(sql, "42601", f"SQL parse error: {exc}")
        if self._is_catalog_query(expression):
            if not self.allow_catalog:
                return _reject(sql, "42501", "Catalog queries are disabled on this QueryPilot proxy.")
            error = self._catalog_error(expression)
            if error is not None:
                return _reject(sql, "42501", error)
            return Statement(kind="catalog", sql=sql, upstream_sql=sql)
        return self._validated(sql, expression)

    def _validated(self, sql: str, expression: exp.Expression) -> Statement:
        outside = self._outside_reference(expression)
        if outside is not None:
            return _reject(sql, "42501", outside)
        validation = self.validator.validate(sql, self.schema)
        if not validation.valid or validation.rewritten_sql is None:
            errors = validation.errors or ["Query rejected by policy."]
            return Statement(
                kind="rejected",
                sql=sql,
                validation=validation,
                sqlstate=_sqlstate(errors[0]),
                message="; ".join(public_errors(errors)),
                errors=errors,
            )
        return Statement(
            kind="query",
            sql=sql,
            upstream_sql=validation.rewritten_sql,
            validation=validation,
            output_masks=list(validation.output_masks),
        )

    def _outside_reference(self, expression: exp.Expression) -> str | None:
        # The validator matches tables by bare name, so other.orders would pass as
        # orders; only the schemas the proxy introspected may be named.
        for table in expression.find_all(exp.Table):
            if table.catalog:
                return f"Database-qualified table references are not allowed: {table.sql('postgres')}"
            if table.db and table.db.lower() not in self.data_schemas:
                return f"Table is outside the proxied schemas: {table.db}.{table.name}"
        return None

    def _is_catalog_query(self, expression: exp.Expression) -> bool:
        """A query that reads only catalog relations (or no relation at all)."""
        if not isinstance(expression, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            return False
        ctes = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
        tables = [table for table in expression.find_all(exp.Table) if isinstance(table.this, exp.Identifier)]
        if not tables:
            # SELECT version(), SELECT current_schema(): catalog only if every function is.
            return self._function_error(expression) is None
        for table in tables:
            name, schema_name = table.name.lower(), table.db.lower()
            if table.catalog:
                return False
            if schema_name:
                if schema_name not in _CATALOG_SCHEMAS:
                    return False
            elif name not in ctes and name not in self.catalog_relations:
                return False
        return True

    def _catalog_error(self, expression: exp.Expression) -> str | None:
        if any(isinstance(node, _WRITE_NODES) for node in expression.walk()):
            return "Catalog queries must be read-only SELECT statements."
        for table in expression.find_all(exp.Table):
            name = table.name.lower()
            if name in _DENIED_CATALOG_RELATIONS or name.startswith(_DENIED_CATALOG_PREFIXES):
                return f"Catalog relation {name} is not available through the QueryPilot proxy."
        return self._function_error(expression)

    def _function_error(self, expression: exp.Expression) -> str | None:
        blocked = {name.lower() for name in self.config.safety_policy.blocked_functions}
        for function in expression.find_all(exp.Func):
            qualifier = _function_qualifier(function)
            name = function.name.lower() if isinstance(function, exp.Anonymous) else function.sql_name().lower()
            if qualifier is not None and qualifier != "pg_catalog":
                return f"Catalog queries may only call pg_catalog functions: {qualifier}.{name}"
            if isinstance(function, exp.Anonymous) and name not in CATALOG_FUNCTIONS:
                return f"Function is not allowed in catalog queries: {name}"
            if name in blocked:
                return f"SQL function is blocked by policy: {name}"
        return None


_WRITE_NODES = (
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Into,
    exp.Lock,
    exp.Merge,
    exp.TruncateTable,
    exp.Update,
)


def _function_qualifier(function: exp.Func) -> str | None:
    parent = function.parent
    if isinstance(parent, exp.Dot) and parent.expression is function:
        return parent.this.name.lower()
    return None


def split_statements(sql: str) -> list[str]:
    """Split a simple-query string on top-level semicolons, keeping each statement's text."""
    try:
        tokens = sqlglot.tokenize(sql, read="postgres")
    except sqlglot.errors.SqlglotError:
        # classify() reports the parse error for the whole string.
        return [sql]
    statements: list[str] = []
    start = 0
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            statements.append(sql[start : token.start])
            start = token.end + 1
    statements.append(sql[start:])
    return [statement for statement in statements if statement.strip()] or [sql]


def _session_command(sql: str, tokens: list[Token]) -> Statement | None:
    words = [token.text.upper() for token in tokens]
    first = words[0]
    if first in ("BEGIN", "START"):
        return _begin(sql, tokens, words)
    if first in ("COMMIT", "END", "ROLLBACK", "ABORT"):
        return _end_transaction(sql, tokens, words)
    if first in ("SAVEPOINT", "RELEASE"):
        return _savepoint(sql, tokens, words)
    if first == "SET":
        return _set(sql, tokens, words)
    if first == "RESET":
        return _reset(sql)
    if first == "SHOW":
        return _show(sql)
    if first == "DEALLOCATE":
        return _deallocate(sql, tokens, words)
    if first in _REJECTED_COMMANDS:
        return _reject(sql, "0A000", _REJECTED_COMMANDS[first])
    return None


_REJECTED_COMMANDS = {
    "COPY": "COPY is not supported by the QueryPilot proxy; use SELECT.",
    "DISCARD": "DISCARD is not supported by the QueryPilot proxy.",
    "DO": "Anonymous code blocks are not allowed.",
    "EXECUTE": "SQL-level PREPARE/EXECUTE is not supported; use the extended query protocol.",
    "EXPLAIN": "EXPLAIN is not supported by the QueryPilot proxy yet.",
    "LISTEN": "LISTEN/NOTIFY is not supported by the QueryPilot proxy.",
    "NOTIFY": "LISTEN/NOTIFY is not supported by the QueryPilot proxy.",
    "PREPARE": "SQL-level PREPARE/EXECUTE is not supported; use the extended query protocol.",
    "UNLISTEN": "LISTEN/NOTIFY is not supported by the QueryPilot proxy.",
}


def _begin(sql: str, tokens: list[Token], words: list[str]) -> Statement:
    rest = words[1:]
    if words[0] == "START":
        if not rest or rest[0] != "TRANSACTION":
            return _reject(sql, "42601", "Unsupported START statement.")
        rest = rest[1:]
    elif rest and rest[0] in ("WORK", "TRANSACTION"):
        rest = rest[1:]
    modes, error = _transaction_modes(rest)
    if error is not None:
        return _reject(sql, error[0], error[1])
    return Statement(kind="begin", sql=sql, upstream_sql=" ".join(["BEGIN", *modes, "READ ONLY"]), tag="BEGIN")


def _transaction_modes(words: list[str]) -> tuple[list[str], tuple[str, str] | None]:
    """Keep the isolation level and DEFERRABLE; READ ONLY is always added by the caller."""
    text = " ".join(word for word in words if word != ",")
    modes: list[str] = []
    while text:
        if text.startswith("ISOLATION LEVEL "):
            text = text[len("ISOLATION LEVEL ") :]
            level = next((level for level in _ISOLATION_LEVELS if text.startswith(level)), None)
            if level is None:
                return [], ("42601", "Unknown isolation level.")
            modes.append(f"ISOLATION LEVEL {level}")
            text = text[len(level) :].strip()
        elif text.startswith("READ WRITE"):
            return [], ("25006", "The QueryPilot proxy only allows read-only transactions.")
        elif text.startswith("READ ONLY"):
            text = text[len("READ ONLY") :].strip()
        elif text.startswith("NOT DEFERRABLE"):
            text = text[len("NOT DEFERRABLE") :].strip()
        elif text.startswith("DEFERRABLE"):
            modes.append("DEFERRABLE")
            text = text[len("DEFERRABLE") :].strip()
        else:
            return [], ("42601", f"Unsupported transaction mode: {text.split()[0]}")
    return modes, None


def _end_transaction(sql: str, tokens: list[Token], words: list[str]) -> Statement:
    rollback = words[0] in ("ROLLBACK", "ABORT")
    rest = words[1:]
    if rest and rest[0] in ("WORK", "TRANSACTION"):
        rest = rest[1:]
    if rollback and rest and rest[0] == "TO":
        rest = rest[1:]
        if rest and rest[0] == "SAVEPOINT":
            rest = rest[1:]
        name = _identifier(tokens[-1]) if len(rest) == 1 else None
        if name is None:
            return _reject(sql, "42601", "Unsupported ROLLBACK TO statement.")
        return Statement(kind="savepoint", sql=sql, upstream_sql=f"ROLLBACK TO SAVEPOINT {name}", tag="ROLLBACK")
    if rest == ["AND", "NO", "CHAIN"]:
        rest = []
    if rest:
        return _reject(sql, "0A000", "Unsupported transaction command (AND CHAIN is not supported).")
    kind = "rollback" if rollback else "commit"
    return Statement(kind=kind, sql=sql, upstream_sql="ROLLBACK" if rollback else "COMMIT", tag=kind.upper())


def _savepoint(sql: str, tokens: list[Token], words: list[str]) -> Statement:
    rest = words[1:]
    if words[0] == "RELEASE" and rest and rest[0] == "SAVEPOINT":
        rest = rest[1:]
    name = _identifier(tokens[-1]) if len(rest) == 1 else None
    if name is None:
        return _reject(sql, "42601", f"Unsupported {words[0]} statement.")
    command = "SAVEPOINT" if words[0] == "SAVEPOINT" else "RELEASE SAVEPOINT"
    return Statement(kind="savepoint", sql=sql, upstream_sql=f"{command} {name}", tag=words[0])


def _set(sql: str, tokens: list[Token], words: list[str]) -> Statement:
    index = 1
    scope = ""
    if index < len(words) and words[index] in ("SESSION", "LOCAL"):
        if words[index] == "SESSION" and words[index + 1 : index + 2] == ["CHARACTERISTICS"]:
            return _set_transaction(sql, words[index + 2 :], "SET SESSION CHARACTERISTICS AS TRANSACTION")
        scope = "LOCAL " if words[index] == "LOCAL" else ""
        index += 1
    if words[index : index + 1] == ["TRANSACTION"]:
        return _set_transaction(sql, words[index:], "SET TRANSACTION")
    if words[index : index + 2] == ["TIME", "ZONE"]:
        name, index = "timezone", index + 2
    elif index < len(tokens) and tokens[index].token_type in (TokenType.VAR, TokenType.IDENTIFIER):
        name, index = tokens[index].text.lower(), index + 1
        if index < len(words) and words[index] in ("TO", "="):
            index += 1
    else:
        return _reject(sql, "42601", "Unsupported SET statement.")
    if name not in SETTABLE:
        return _reject(sql, "42501", f"SET {name} is not allowed through the QueryPilot proxy.")
    value = _setting_value(tokens[index:])
    if value is None:
        return _reject(sql, "42601", f"Unsupported value for SET {name}.")
    allowed = _FIXED_VALUES.get(name)
    if allowed is not None and value.strip("'").lower() not in allowed:
        return _reject(sql, "0A000", f"The QueryPilot proxy only supports {name} = {min(allowed)}.")
    return Statement(kind="set", sql=sql, upstream_sql=f"SET {scope}{name} TO {value}", tag="SET")


def _set_transaction(sql: str, words: list[str], prefix: str) -> Statement:
    rest = words[1:] if words[:1] == ["TRANSACTION"] else words
    if rest[:1] == ["SNAPSHOT"]:
        return _reject(sql, "0A000", "SET TRANSACTION SNAPSHOT is not supported.")
    modes, error = _transaction_modes(rest)
    if error is not None:
        return _reject(sql, error[0], error[1])
    return Statement(kind="set", sql=sql, upstream_sql=" ".join([prefix, *modes, "READ ONLY"]), tag="SET")


def _setting_value(tokens: list[Token]) -> str | None:
    """Re-quote a SET value list; only literals and bare words are accepted."""
    if len(tokens) == 1 and tokens[0].token_type == TokenType.DEFAULT:
        return "DEFAULT"
    parts: list[str] = []
    negative = False
    expect_value = True
    for token in tokens:
        if expect_value and token.token_type == TokenType.DASH and not negative:
            negative = True
            continue
        if expect_value:
            if token.token_type == TokenType.NUMBER:
                parts.append(("-" if negative else "") + token.text)
            elif token.token_type in (TokenType.STRING, TokenType.VAR, TokenType.IDENTIFIER) and not negative:
                parts.append(_quote_literal(token.text))
            else:
                return None
            negative = False
            expect_value = False
        elif token.token_type == TokenType.COMMA:
            expect_value = True
        else:
            return None
    return ", ".join(parts) if parts and not expect_value else None


def _reset(sql: str) -> Statement:
    # sqlglot reads everything after RESET as one string token, so use the raw text.
    name = " ".join(sql.strip().rstrip(";").split()[1:]).lower()
    if name not in SETTABLE:
        return _reject(sql, "42501", f"RESET {name} is not allowed through the QueryPilot proxy.")
    return Statement(kind="set", sql=sql, upstream_sql=f"RESET {name}", tag="RESET")


def _show(sql: str) -> Statement:
    body = " ".join(sql.strip().rstrip(";").split()[1:]).lower()
    name = _SETTING_ALIASES.get(body, body)
    if name not in SHOWABLE:
        return _reject(sql, "42501", f"SHOW {body} is not allowed through the QueryPilot proxy.")
    return Statement(kind="show", sql=sql, upstream_sql=f"SHOW {name}")


def _deallocate(sql: str, tokens: list[Token], words: list[str]) -> Statement:
    rest = tokens[1:]
    if rest and words[1] == "PREPARE":
        rest = rest[1:]
    if len(rest) != 1:
        return _reject(sql, "42601", "Unsupported DEALLOCATE statement.")
    target = None if rest[0].text.upper() == "ALL" else rest[0].text
    return Statement(kind="deallocate", sql=sql, tag="DEALLOCATE ALL" if target is None else "DEALLOCATE", target=target)


def _identifier(token: Token) -> str | None:
    if token.token_type not in (TokenType.VAR, TokenType.IDENTIFIER):
        return None
    return '"' + token.text.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _reject(sql: str, sqlstate: str, message: str) -> Statement:
    return Statement(kind="rejected", sql=sql, sqlstate=sqlstate, message=message, errors=[message])


def _sqlstate(error: str) -> str:
    if error.startswith("SQL parse error"):
        return "42601"
    if error.startswith(("Only SELECT", "SQL contains a non-read-only", "Multiple SQL")):
        return "25006"
    if error.startswith("Unknown table"):
        return "42P01"
    if error.startswith(("Unknown column", "Ambiguous column", "Unknown table qualifier")):
        return "42703"
    return "42501"
