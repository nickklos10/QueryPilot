"""Classify what a query tool returned: success, failure class, or not run.

Foundry's SQL tool reports query failures as ordinary text ("Sql Query
Failed ... [UNRESOLVED_COLUMN...]"), not as tool errors, so classification reads
the text. Patterns cover Spark SQL error classes, Foundry HTTP errors, MCP
transport failures and Claude Code's own messages (oversized results, denied
calls); other engines' messages fall through to the generic patterns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

UNRESOLVED_COLUMN = "unresolved_column"
UNKNOWN_TABLE = "unknown_table"
TYPE_MISMATCH = "type_mismatch"
RESULT_TOO_LARGE = "result_too_large"
TIMEOUT = "timeout"
UPSTREAM = "upstream"
SYNTAX = "syntax"
SQL_ANALYSIS = "sql_analysis"
TOOL_INPUT = "tool_input"
OTHER = "other"

CLASS_LABELS = {
    UNRESOLVED_COLUMN: "Unresolved column",
    UNKNOWN_TABLE: "Unknown table / dataset",
    TYPE_MISMATCH: "Data type mismatch",
    RESULT_TOO_LARGE: "Result too large for context",
    TIMEOUT: "Timeout",
    UPSTREAM: "Upstream / network (no SQL error returned)",
    SYNTAX: "Syntax / parse",
    SQL_ANALYSIS: "Other SQL analysis error",
    TOOL_INPUT: "Invalid tool call",
    OTHER: "Other",
}

# Call outcomes.
OK = "ok"
ERROR = "error"
# The user, a permission rule or another hook refused the call; it never ran.
DENIED = "denied"
# The QueryPilot guard refused the call before it ran.
BLOCKED = "blocked"
# No tool_result was recorded (interrupted session or still running).
NO_RESULT = "no_result"

GUARD_PREFIX = "QueryPilot guard"

_DENIED = re.compile(
    r"doesn't want to proceed with this tool use|Permission for this action was denied|"
    r"denied by (?:the )?(?:user|hook|PreToolUse)|tool use was rejected",
    re.IGNORECASE,
)
_TOO_LARGE = re.compile(r"exceeds maximum allowed tokens|response too large|result is too large", re.IGNORECASE)
_SPILLED = re.compile(r"^<persisted-output>|Output too large \(", re.IGNORECASE)
# Only the start of the text: result rows can contain any of these words.
_FAILED_SQL = re.compile(
    r"^\s*(?:Sql Query Failed|Error\b|MCP error|<tool_use_error>|Failed to |fetch failed)|"
    r"Failed to execute SQL",
    re.IGNORECASE,
)

# Ordered: specific error classes before generic transport text, because Foundry
# wraps some engine errors ("Failed writing blob data ... [CAST_INVALID_INPUT]").
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (TOOL_INPUT, re.compile(r"Input validation error|No such tool available|Invalid arguments for tool", re.IGNORECASE)),
    (
        UNRESOLVED_COLUMN,
        re.compile(
            r"UNRESOLVED_COLUMN|CANNOT_RESOLVE_STAR_EXPAND|UNRESOLVED_FIELD|AMBIGUOUS_REFERENCE|"
            r"no such column|column \"?[\w.]+\"? does not exist|unknown column|invalid identifier",
            re.IGNORECASE,
        ),
    ),
    (
        UNKNOWN_TABLE,
        re.compile(
            r"TABLE_OR_VIEW_NOT_FOUND|DatasetNotFound|referenced dataset, schema, or table could not be found|"
            r"dataset RID is incorrect|ReadQueryInputsPermissionDenied|no such table|"
            r"relation \"?[\w.]+\"? does not exist|table '[^']+' doesn't exist",
            re.IGNORECASE,
        ),
    ),
    (
        TYPE_MISMATCH,
        re.compile(
            r"DATATYPE_MISMATCH|CAST_INVALID_INPUT|INCOMPATIBLE_DATA_TYPE|"
            r"value used by the query could not be parsed|cannot be cast to|operator does not exist",
            re.IGNORECASE,
        ),
    ),
    (SYNTAX, re.compile(r"QueryParseError|PARSE_SYNTAX_ERROR|mismatched input|syntax error|cannot be parsed", re.IGNORECASE)),
    (
        TIMEOUT,
        re.compile(r"timed? ?out|timeout|deadline exceeded|statement cancel|canceled because it wasn't finished", re.IGNORECASE),
    ),
    (
        UPSTREAM,
        re.compile(
            r"fetch failed|Failed to fetch 5\d\d|INTERNAL_ERROR|internal error|\"type\": ?\"NETWORK\"|"
            r"ECONNRESET|ETIMEDOUT|socket hang up|Service Unavailable|Bad Gateway|errorCode\":\"INTERNAL|"
            r"Refer to the server with this errorInstanceId",
            re.IGNORECASE,
        ),
    ),
    (
        SQL_ANALYSIS,
        re.compile(
            r"\[[A-Z][A-Z_]+(?:\.[A-Z_]+)?\]|failed analysis|SqlAnalyzerException|Failed to fetch 4\d\d", re.IGNORECASE
        ),
    ),
]


_HTTP_STATUS = re.compile(r"Failed to fetch (\d{3})\b")


@dataclass(frozen=True)
class Outcome:
    status: str
    error_class: str | None = None
    row_count: int | None = None
    # Claude Code saved the result to a file and showed the model a preview.
    spilled: bool = False


def classify_output(text: str, *, is_error: bool = False) -> Outcome:
    head = text[:4000]
    # Claude Code may wrap a hook's deny reason in its own text.
    if GUARD_PREFIX in head[:500]:
        return Outcome(BLOCKED)
    if _DENIED.search(head[:400]):
        return Outcome(DENIED)
    if _TOO_LARGE.search(head[:400]):
        return Outcome(ERROR, RESULT_TOO_LARGE)
    if not is_error and not _FAILED_SQL.search(head[:300]):
        return Outcome(OK, row_count=markdown_row_count(text), spilled=bool(_SPILLED.search(head[:200])))
    return Outcome(ERROR, error_class(head))


def error_class(text: str) -> str:
    # Foundry HTTP errors: the MCP server appends a "403 Forbidden ... RID is
    # incorrect" suggestion to every failure, so the status code decides first.
    http = _HTTP_STATUS.search(text[:200])
    if http:
        status = int(http.group(1))
        if status >= 500:
            return UPSTREAM
        if status in (403, 404):
            return UNKNOWN_TABLE
        if "{" not in text[:400]:
            # A bare "Failed to fetch 400" plus a stack trace: the tool dropped the error body.
            return UPSTREAM
    for name, pattern in _RULES:
        if pattern.search(text):
            return name
    return OTHER


def markdown_row_count(text: str) -> int | None:
    """Rows in a markdown table result (header and separator excluded)."""
    lines = [line for line in text.splitlines() if line.startswith("|")]
    if len(lines) < 2 or not set(lines[1].replace("|", "").strip()) <= {"-", ":", " "}:
        return None
    return len(lines) - 2


def markdown_header(text: str) -> list[str] | None:
    lines = [line for line in text.splitlines() if line.startswith("|")]
    if len(lines) < 2 or not set(lines[1].replace("|", "").strip()) <= {"-", ":", " "}:
        return None
    return [cell.strip() for cell in lines[0].strip().strip("|").split("|")]
