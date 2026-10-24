"""Diagnose an agent's query-tool history and say where each problem is best fixed.

Every finding is tagged with the layer that should change:

- **tool**: QueryPilot's guard or the tool itself (validate before running,
  cap rows, retry transport errors, render dates readably);
- **prompt**: a hint the agent should get up front (dialect rules, the real
  name of a column it keeps guessing);
- **data**: the dataset should describe or model itself better (a column
  description, a view with a proper DATE column).

The guard replay walks the history in time order, building the schema cache
the way the hooks would, and asks what the guard would have done with each
query: how many failures it would have stopped before they reached the
warehouse, and how many successful queries it would have blocked by mistake.
"""
from __future__ import annotations

import difflib
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import sqlglot
from sqlglot import exp

from querypilot.audit.types import QueryAuditRecord
from querypilot.trace.classify import (
    BLOCKED,
    CLASS_LABELS,
    DENIED,
    ERROR,
    NO_RESULT,
    OK,
    RESULT_TOO_LARGE,
    SQL_ANALYSIS,
    SYNTAX,
    TIMEOUT,
    TOOL_INPUT,
    TYPE_MISMATCH,
    UNKNOWN_TABLE,
    UNRESOLVED_COLUMN,
    UPSTREAM,
)
from querypilot.trace.guard import guard_sql
from querypilot.trace.schema_cache import (
    FOUNDRY_RID,
    SchemaCache,
    table_ref_name,
    table_refs,
)
from querypilot.trace.transcript import QUERY, SCHEMA, ToolCall

TOOL_FIX = "tool"
PROMPT_FIX = "prompt"
DATA_FIX = "data"

# Where each failure class is best fixed first.
PRIMARY_FIX = {
    UNRESOLVED_COLUMN: TOOL_FIX,
    UNKNOWN_TABLE: TOOL_FIX,
    TYPE_MISMATCH: DATA_FIX,
    RESULT_TOO_LARGE: TOOL_FIX,
    TIMEOUT: TOOL_FIX,
    UPSTREAM: TOOL_FIX,
    SYNTAX: PROMPT_FIX,
    SQL_ANALYSIS: PROMPT_FIX,
    TOOL_INPUT: PROMPT_FIX,
}

_UNRESOLVED = re.compile(
    r"with name (`[^`]+`(?:\.`[^`]+`)*) cannot be resolved\.(?: Did you mean one of the following\? \[([^\]]*)\])?"
)
_MISMATCH = re.compile(r'Cannot resolve "(.+?)" due to data type mismatch.*?\("([A-Z]+)" and "([A-Z]+)"\)', re.DOTALL)
_BACKTICKED = re.compile(r"`([^`]+)`")


@dataclass
class ClassRow:
    error_class: str
    label: str
    count: int
    share_of_calls: float
    share_of_failures: float
    fix: str
    sessions: int


@dataclass
class ChainStats:
    chains: int = 0
    recovered: int = 0
    abandoned: int = 0
    failed_attempts: int = 0
    max_attempts: int = 0
    attempts_histogram: dict[str, int] = field(default_factory=dict)
    schema_fetched_in_chain: int = 0
    wasted_seconds: float = 0.0
    recovery_seconds_p50: float | None = None
    by_first_class: dict[str, int] = field(default_factory=dict)
    duplicate_successes: int = 0


@dataclass
class LatencyStats:
    measured: int = 0
    p50: float | None = None
    p90: float | None = None
    p99: float | None = None
    max: float | None = None
    stall_seconds: float = 60.0
    stalls: int = 0
    stalls_failed: int = 0
    over_300s: int = 0
    no_result: int = 0
    by_class_p50: dict[str, float] = field(default_factory=dict)


@dataclass
class GuardReplay:
    checked: int = 0
    caught: dict[str, int] = field(default_factory=dict)
    false_blocks: int = 0
    successes_checked: int = 0
    false_block_examples: list[str] = field(default_factory=list)
    caught_examples: list[str] = field(default_factory=list)
    cached_datasets: int = 0
    limit_missing_in_too_large: int = 0
    select_star_in_too_large: int = 0
    # Oversized results the row cap or the wide SELECT * rule would have stopped.
    size_caught: int = 0
    wide_star_in_too_large: int = 0
    # Successful queries those two rules would have sent back for a LIMIT or named columns.
    size_blocks_of_successes: int = 0
    too_large_row_limits: dict[str, int] = field(default_factory=dict)


@dataclass
class Finding:
    fix: str
    title: str
    impact: int
    detail: str
    suggestion: str
    examples: list[str] = field(default_factory=list)


@dataclass
class DoctorReport:
    tools: list[str]
    calls: int
    sessions: int
    threads: int
    ran: int
    failed: int
    denied: int
    blocked: int
    no_result: int
    failure_rate: float
    first_call: str | None
    last_call: str | None
    classes: list[ClassRow]
    chains: ChainStats
    latency: LatencyStats
    guard: GuardReplay
    findings: list[Finding]
    hints: list[str]
    descriptions: dict[str, str]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


# --------------------------------------------------------------------------- redaction


class Redactor:
    """Stable labels for datasets and '?' for literals, so reports can be shared."""

    def __init__(self, enabled: bool = True, cache: SchemaCache | None = None) -> None:
        self.enabled = enabled
        self.cache = cache
        self._labels: dict[str, str] = {}

    def dataset(self, ref: str) -> str:
        ref = ref.strip("`")
        if not self.enabled:
            hit = self.cache.lookup(ref) if self.cache else None
            return hit.display_name if hit else ref
        hit = self.cache.lookup(ref) if self.cache else None
        key = hit.key if hit else ref
        if key not in self._labels:
            self._labels[key] = f"dataset_{_letters(len(self._labels))}"
        return self._labels[key]

    def sql(self, sql: str | None, dialect: str = "spark", limit: int = 240) -> str:
        if not sql:
            return ""
        if not self.enabled:
            return _one_line(sql)[:limit]
        try:
            expression = sqlglot.parse_one(sql, read=dialect)
            for table in table_refs(expression):
                table.set("this", exp.to_identifier(self.dataset(table_ref_name(table))))
                table.set("db", None)
                table.set("catalog", None)
            for literal in list(expression.find_all(exp.Literal)):
                if literal.is_string:
                    literal.replace(exp.Literal.string("?"))
            text = expression.sql(dialect=dialect)
        except Exception:  # noqa: BLE001 - fall back to text redaction
            text = self.text(sql)
        return _one_line(text)[:limit]

    def guard_text(self, text: str) -> str:
        """A guard reply with dataset names removed; its quoted words are column names, not data."""
        if not self.enabled:
            return _one_line(text)[:240]
        text = re.sub(r"\b[A-Za-z_]\w*\.(?=[A-Za-z_])", "", text)
        text = re.sub(
            r"did you mean ([^?]+)\?",
            lambda m: "did you mean " + ", ".join(dict.fromkeys(m.group(1).split(", "))) + "?",
            text,
        )
        text = FOUNDRY_RID.sub(lambda m: self.dataset(m.group(0)), text)
        return _one_line(re.sub(r"/[\w .-]+(?:/[\w .-]+)+", "<path>", text))[:240]

    def text(self, text: str, limit: int = 240) -> str:
        if not self.enabled:
            return _one_line(text)[:limit]
        text = FOUNDRY_RID.sub(lambda m: self.dataset(m.group(0)), text)
        text = re.sub(r"(?<![\w'])'[^'\n]*'", "'?'", text)
        text = re.sub(r"/[\w .-]+(?:/[\w .-]+)+", "<path>", text)
        return _one_line(text)[:limit]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _letters(n: int) -> str:
    out = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- analysis


def diagnose(
    calls: list[ToolCall],
    *,
    stall_seconds: float = 60.0,
    max_rows: int = 200,
    max_star_columns: int | None = 20,
    redact: bool = True,
) -> DoctorReport:
    queries = [c for c in calls if c.kind == QUERY]
    ran = [c for c in queries if c.ran and c.status != NO_RESULT]
    failed = [c for c in ran if c.status == ERROR]
    cache = SchemaCache()
    guard = _replay_guard(calls, cache, max_rows=max_rows, max_star_columns=max_star_columns)
    redactor = Redactor(redact, cache)
    guard.caught_examples = [redactor.guard_text(e) for e in guard.caught_examples]
    guard.false_block_examples = [redactor.guard_text(e) for e in guard.false_block_examples]

    classes = _class_rows(failed, len(ran))
    chains, _ = retry_chains(calls)
    latency = _latency(queries, stall_seconds)
    findings, hints, descriptions = _findings(calls, failed, classes, chains, latency, guard, cache, redactor)
    starts = [c.started_at for c in queries if c.started_at]
    return DoctorReport(
        tools=sorted({c.tool for c in queries}),
        calls=len(queries),
        sessions=len({c.session_id for c in queries}),
        threads=len({c.thread for c in queries}),
        ran=len(ran),
        failed=len(failed),
        denied=sum(c.status == DENIED for c in queries),
        blocked=sum(c.status == BLOCKED for c in queries),
        no_result=sum(c.status == NO_RESULT for c in queries),
        failure_rate=len(failed) / len(ran) if ran else 0.0,
        first_call=min(starts).date().isoformat() if starts else None,
        last_call=max(starts).date().isoformat() if starts else None,
        classes=classes,
        chains=chains,
        latency=latency,
        guard=guard,
        findings=findings,
        hints=hints,
        descriptions=descriptions,
    )


def _class_rows(failed: list[ToolCall], ran: int) -> list[ClassRow]:
    counts = Counter(c.error_class for c in failed)
    sessions: dict[str, set] = defaultdict(set)
    for call in failed:
        sessions[call.error_class].add(call.session_id)
    return [
        ClassRow(
            error_class=name,
            label=CLASS_LABELS.get(name, name),
            count=count,
            share_of_calls=count / ran if ran else 0.0,
            share_of_failures=count / len(failed) if failed else 0.0,
            fix=PRIMARY_FIX.get(name, PROMPT_FIX),
            sessions=len(sessions[name]),
        )
        for name, count in counts.most_common()
    ]


def query_tables(sql: str | None, dialect: str) -> set[str]:
    if not sql:
        return set()
    try:
        expression = sqlglot.parse_one(sql, read=dialect)
        return {table_ref_name(t).lower() for t in table_refs(expression)}
    except Exception:  # noqa: BLE001 - fall back to backticked names
        return {m.lower() for m in _BACKTICKED.findall(sql) if m.startswith(("ri.", "/"))}


def retry_chains(calls: list[ToolCall]) -> tuple[ChainStats, list[tuple[list[ToolCall], ToolCall | None]]]:
    """Failed query -> follow-up queries on the same tables, until one succeeds.

    Returns the stats and each chain as (failed calls, the call that recovered or None).
    """
    stats = ChainStats()
    found: list[tuple[list[ToolCall], ToolCall | None]] = []
    by_thread: dict[str | None, list[ToolCall]] = defaultdict(list)
    for call in calls:
        if call.ran and call.status != NO_RESULT:
            by_thread[call.thread].append(call)
    histogram: Counter = Counter()
    recovery_times: list[float] = []
    first_classes: Counter = Counter()

    for thread_calls in by_thread.values():
        thread_calls.sort(key=lambda c: (c.started_at.timestamp() if c.started_at else 0.0, c.sequence))
        seen_ok: Counter = Counter()
        chain: list[ToolCall] | None = None
        tables: set[str] = set()
        schema_seen = False

        def close(recovered_by: ToolCall | None) -> None:
            nonlocal chain, schema_seen
            if not chain:
                return
            found.append((chain, recovered_by))
            stats.chains += 1
            stats.failed_attempts += len(chain)
            stats.max_attempts = max(stats.max_attempts, len(chain))
            histogram[str(len(chain)) if len(chain) < 5 else "5+"] += 1
            stats.wasted_seconds += sum(c.latency_s or 0 for c in chain)
            first_classes[chain[0].error_class] += 1
            if schema_seen:
                stats.schema_fetched_in_chain += 1
            if recovered_by is not None:
                stats.recovered += 1
                end = recovered_by.ended_at or recovered_by.started_at
                if end and chain[0].started_at:
                    recovery_times.append((end - chain[0].started_at).total_seconds())
            else:
                stats.abandoned += 1
            chain, schema_seen = None, False

        for call in thread_calls:
            if call.kind == SCHEMA:
                if chain:
                    schema_seen = True
                continue
            call_tables = query_tables(call.sql, call.dialect)
            if chain is not None and call_tables and tables and not (call_tables & tables):
                close(None)
            if call.status == ERROR:
                if chain is None:
                    chain, tables = [], set(call_tables)
                chain.append(call)
                tables |= call_tables
            elif call.status == OK:
                close(call)
                key = _normalized(call.sql)
                if key:
                    seen_ok[key] += 1
                    if seen_ok[key] > 1:
                        stats.duplicate_successes += 1
        close(None)

    stats.attempts_histogram = dict(sorted(histogram.items()))
    stats.recovery_seconds_p50 = statistics.median(recovery_times) if recovery_times else None
    stats.by_first_class = dict(first_classes.most_common())
    return stats, found


def _latency(queries: list[ToolCall], stall_seconds: float) -> LatencyStats:
    measured = [c for c in queries if c.ran and c.status != NO_RESULT and c.latency_s is not None]
    values = sorted(c.latency_s for c in measured)
    stats = LatencyStats(measured=len(values), stall_seconds=stall_seconds)
    stats.no_result = sum(c.status == NO_RESULT for c in queries)
    if not values:
        return stats
    stats.p50 = _percentile(values, 50)
    stats.p90 = _percentile(values, 90)
    stats.p99 = _percentile(values, 99)
    stats.max = values[-1]
    stalls = [c for c in measured if c.latency_s > stall_seconds]
    stats.stalls = len(stalls)
    stats.stalls_failed = sum(c.status == ERROR for c in stalls)
    stats.over_300s = sum(c.latency_s > 300 for c in measured)
    by_class: dict[str, list[float]] = defaultdict(list)
    for call in measured:
        by_class[call.error_class or "ok"].append(call.latency_s)
    stats.by_class_p50 = {k: round(statistics.median(v), 1) for k, v in sorted(by_class.items())}
    return stats


def _percentile(values: list[float], pct: float) -> float:
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * pct / 100
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (rank - low)


def _replay_guard(
    calls: list[ToolCall], cache: SchemaCache, *, max_rows: int, max_star_columns: int | None = None
) -> GuardReplay:
    replay = GuardReplay()
    caught: Counter = Counter()
    row_limits: Counter = Counter()
    for call in calls:
        if call.kind == SCHEMA:
            if call.dataset is not None:
                cache.add(call.dataset)
            continue
        if not call.sql or not call.ran or call.status == NO_RESULT:
            continue
        decision = guard_sql(call.sql, cache, dialect=call.dialect)
        # The size rules judged against the cache as it stood before this call.
        sized = guard_sql(
            call.sql,
            cache,
            dialect=call.dialect,
            max_rows=max_rows,
            tool_row_limit=_int(call.params.get("rowLimit")),
            max_star_columns=max_star_columns,
        )
        size_denied = not decision.denied and sized.rule in ("star", "limit")
        if decision.checked_columns:
            replay.checked += 1
        if call.status == OK:
            replay.successes_checked += decision.checked_columns
            if decision.denied:
                # Live, the agent resends the query, it succeeds, and the hook evicts the stale entry.
                replay.false_blocks += 1
                if len(replay.false_block_examples) < 8:
                    replay.false_block_examples.append(_first_line(decision.reason))
                cache.evict(decision.tables)
            replay.size_blocks_of_successes += size_denied
            if call.header:
                cache.learn_from_select_star(call.sql, call.header, call.dialect)
        elif decision.denied:
            caught[call.error_class] += 1
            if len(replay.caught_examples) < 5:
                replay.caught_examples.append(_first_line(decision.reason))
        if call.error_class == RESULT_TOO_LARGE:
            limited = guard_sql(call.sql, SchemaCache(), dialect=call.dialect, max_rows=max_rows,
                                tool_row_limit=_int(call.params.get("rowLimit")))
            replay.limit_missing_in_too_large += limited.rule == "limit"
            replay.wide_star_in_too_large += sized.rule == "star"
            replay.size_caught += size_denied
            replay.select_star_in_too_large += bool(re.search(r"select\s+(?:\w+\.)?\*", call.sql, re.IGNORECASE))
            row_limits[_bucket(_int(call.params.get("rowLimit")))] += 1
    replay.caught = dict(caught.most_common())
    replay.cached_datasets = len(cache)
    replay.too_large_row_limits = {k: row_limits[k] for k in _BUCKETS if k in row_limits}
    return replay


_BUCKETS = ("<= 50", "<= 100", "<= 200", "<= 500", "<= 1000", "> 1000", "not set")


def _bucket(value: int | None) -> str:
    if value is None:
        return "not set"
    for bound in (50, 100, 200, 500, 1000):
        if value <= bound:
            return f"<= {bound}"
    return "> 1000"


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_line(text: str | None) -> str:
    """The guard's first problem, without the column listing that follows it."""
    lines = [line for line in (text or "").splitlines() if line.startswith("- ")]
    first = lines[0][2:] if lines else (text or "")
    return first.split(" Columns of ")[0]


def _normalized(sql: str | None) -> str | None:
    return re.sub(r"\s+", " ", sql.strip().lower()) if sql else None


# --------------------------------------------------------------------------- findings


def _findings(
    calls: list[ToolCall],
    failed: list[ToolCall],
    classes: list[ClassRow],
    chains: ChainStats,
    latency: LatencyStats,
    guard: GuardReplay,
    cache: SchemaCache,
    redactor: Redactor,
) -> tuple[list[Finding], list[str], dict[str, str]]:
    counts = {row.error_class: row.count for row in classes}
    findings: list[Finding] = []
    hints: list[str] = []
    descriptions: dict[str, str] = {}

    # Unresolved columns: guard first, then recurring guesses as prompt/data fixes.
    if counts.get(UNRESOLVED_COLUMN):
        n = counts[UNRESOLVED_COLUMN]
        caught = guard.caught.get(UNRESOLVED_COLUMN, 0)
        findings.append(
            Finding(
                TOOL_FIX,
                "Validate columns before the query runs (QueryPilot guard)",
                n,
                f"{n} queries referenced a column the dataset does not have. Replaying the history, "
                f"the guard (schema cache built from earlier schema calls and SELECT * results) would have "
                f"stopped {caught} of them before they reached the warehouse, with a 'did you mean' reply, "
                f"and wrongly blocked {guard.false_blocks} of {guard.successes_checked} successful queries it "
                "could check. The rest touched datasets with no cached schema.",
                "querypilot hooks install --guard (optionally --require-schema, which makes the agent fetch "
                "a dataset's schema before its first query).",
                guard.caught_examples[:3],
            )
        )
        guesses = _column_guesses(failed)
        recurring = [(wrong, data) for wrong, data in guesses if data["count"] >= 2]
        if recurring:
            lines = []
            for wrong, data in recurring[:8]:
                best = data["best"]
                tables = ", ".join(sorted({redactor.dataset(t) for t in data["tables"]})[:2]) or "its dataset"
                lines.append(
                    f"`{wrong}` x{data['count']} in {_plural(len(data['sessions']), 'session')} "
                    f"({tables}); closest real columns: {', '.join(data['closest'][:3]) or 'none listed'}"
                )
                if best:
                    hints.append(f"In {tables}, there is no `{wrong}` column; use `{best}`.")
            findings.append(
                Finding(
                    PROMPT_FIX,
                    "Tell the agent the real names of columns it keeps guessing",
                    sum(d["count"] for _, d in recurring),
                    "The same wrong column name failed repeatedly, often across sessions, so the agent is "
                    "not carrying the schema forward. Recurring guesses:",
                    "Add the hints below to the agent's instructions (CLAUDE.md, or `hints:` in "
                    "querypilot.yaml); when a guess recurs across many sessions, the data fix is a column "
                    "description or an alias column in a view.",
                    lines,
                )
            )
        schema_after = chains.schema_fetched_in_chain
        if schema_after:
            findings.append(
                Finding(
                    PROMPT_FIX,
                    "Fetch the schema before the first query, not after it fails",
                    schema_after,
                    f"In {schema_after} retry chains the agent fetched a dataset schema only after a query "
                    "failed.",
                    "Instruct: 'Before querying a Foundry dataset for the first time in a session, call "
                    "get_foundry_dataset_schema and use its exact column names.' The guard's --require-schema "
                    "enforces this.",
                )
            )

    if counts.get(UNKNOWN_TABLE):
        n = counts[UNKNOWN_TABLE]
        findings.append(
            Finding(
                TOOL_FIX,
                "Catch wrong or inaccessible dataset references",
                n,
                f"{n} queries named a dataset that was not found or not readable (Foundry answers 403 for "
                f"both a wrong RID and missing access). The guard caught {guard.caught.get(UNKNOWN_TABLE, 0)} "
                "(malformed RIDs).",
                "Keep a registry of the datasets the agent uses (RID, path, one-line purpose) in its "
                "instructions, and enable --require-schema so a typo fails at the cheap schema call.",
            )
        )

    if counts.get(TYPE_MISMATCH):
        n = counts[TYPE_MISMATCH]
        epoch = _epoch_mismatches(failed, cache)
        epoch_total = sum(epoch.values())
        rendered = _epoch_rendered(calls, cache)
        examples = [
            f"`{col}` ({kind}, {redactor.dataset(table)}) x{count}" for (col, kind, table), count in epoch.most_common(8)
        ]
        for col, kind, table in list(epoch)[:12]:
            descriptions[f"{redactor.dataset(table)}.{col}"] = (
                f"{kind}. Not epoch milliseconds, although query results display it as a 13-digit number; "
                f"filter with DATE literals (e.g. {col} >= DATE '2026-01-01') or date functions."
            )
        if epoch_total:
            hints.append(
                "Foundry SQL results show DATE/TIMESTAMP values as epoch-millisecond numbers, but the "
                "columns are real dates: never divide them by 1000 or compare them with 13-digit numbers; "
                "use DATE 'YYYY-MM-DD' literals and date functions."
            )
        findings.append(
            Finding(
                DATA_FIX,
                "Date columns read as epoch milliseconds",
                epoch_total or n,
                f"{n} queries failed on a data type mismatch; {epoch_total} of them treated a DATE or "
                f"TIMESTAMP column as epoch milliseconds (`col / 1000`, `col >= 1767225600000`). "
                f"{rendered} successful results displayed a DATE/TIMESTAMP column as 13-digit numbers, "
                "which is where the agent learns the wrong type.",
                "Data: add column descriptions that state the type (suggested `descriptions:` below), or "
                "publish views with explicit DATE columns. Tool: the PostToolUse hook annotates results whose "
                "date columns are rendered as epoch ms, and the guard blocks `date_col / 1000`.",
                examples,
            )
        )

    if counts.get(RESULT_TOO_LARGE):
        n = counts[RESULT_TOO_LARGE]
        findings.append(
            Finding(
                TOOL_FIX,
                "Cap result size before it floods the context window",
                n,
                f"{n} results exceeded Claude Code's output limit. {guard.limit_missing_in_too_large} had "
                f"neither a LIMIT nor a rowLimit at or below the cap, and {guard.select_star_in_too_large} "
                f"used SELECT *. rowLimit on those calls: "
                + ", ".join(f"{k}: {v}" for k, v in guard.too_large_row_limits.items())
                + ". Wide text columns overflow even at 100 rows.",
                "querypilot hooks install --guard --max-rows 200 enforces a cap; the prompt fix is to "
                "select only the needed columns and aggregate in SQL instead of reading raw rows.",
            )
        )

    if latency.stalls or counts.get(TIMEOUT):
        findings.append(
            Finding(
                TOOL_FIX,
                "Long-running queries stall the agent",
                latency.stalls,
                f"{latency.stalls} calls took longer than {latency.stall_seconds:.0f}s "
                f"({latency.over_300s} over 5 minutes); {latency.stalls_failed} of those failed anyway. "
                f"{counts.get(TIMEOUT, 0)} calls failed with a timeout.",
                "Cap rows and require a filter on the partition/date column for large datasets; for queries "
                "the agent repeats, materialize the result as a dataset or view (data fix).",
            )
        )

    if counts.get(UPSTREAM):
        n = counts[UPSTREAM]
        findings.append(
            Finding(
                TOOL_FIX,
                "Transport and server errors reach the agent as opaque failures",
                n,
                f"{n} calls failed without a SQL error: network fetch failures, Foundry internal errors, and "
                "HTTP errors whose body the MCP tool dropped (a stack trace instead of a message).",
                "Fix in the tool layer: retry with backoff on network and 5xx errors and surface Foundry's "
                "error body. The agent cannot fix these by rewriting SQL.",
            )
        )

    syntax_like = counts.get(SYNTAX, 0) + counts.get(SQL_ANALYSIS, 0)
    if syntax_like:
        codes = Counter(
            m.group(1) for c in failed if c.error_class in (SYNTAX, SQL_ANALYSIS)
            for m in [re.search(r"\[([A-Z][A-Z_]+)(?:\.[A-Z_]+)?\]", c.output)] if m
        )
        findings.append(
            Finding(
                PROMPT_FIX,
                "Spark SQL dialect mistakes",
                syntax_like,
                f"{syntax_like} queries failed to parse or failed Spark analysis "
                + (f"({', '.join(f'{k} x{v}' for k, v in codes.most_common(5))})." if codes else "."),
                "State the dialect in the agent's instructions: 'Foundry SQL is Spark SQL: quote dataset "
                "RIDs in backticks, every non-aggregated column must be in GROUP BY, use try_cast/try_divide.'",
            )
        )
        hints.append(
            "Foundry SQL is Spark SQL: quote dataset RIDs and paths in backticks, list every "
            "non-aggregated column in GROUP BY, and prefer try_cast / try_divide."
        )

    if chains.duplicate_successes:
        findings.append(
            Finding(
                TOOL_FIX,
                "Identical queries re-run in the same session",
                chains.duplicate_successes,
                f"{chains.duplicate_successes} successful queries repeated a query that had already succeeded "
                "earlier in the same session.",
                "Cache results per session in the tool, or tell the agent to keep earlier results instead of "
                "re-querying.",
            )
        )

    order = {TOOL_FIX: 0, PROMPT_FIX: 1, DATA_FIX: 2}
    findings.sort(key=lambda f: (-f.impact, order[f.fix]))
    return findings, list(dict.fromkeys(hints)), descriptions


def _column_guesses(failed: list[ToolCall]) -> list[tuple[str, dict]]:
    out: dict[str, dict] = {}
    for call in failed:
        if call.error_class != UNRESOLVED_COLUMN:
            continue
        match = _UNRESOLVED.search(call.output)
        if not match:
            continue
        wrong = match.group(1).replace("`", "").split(".")[-1]
        suggestions = [s.strip("` ").split(".")[-1] for s in (match.group(2) or "").split(",") if s.strip()]
        entry = out.setdefault(
            wrong.lower(), {"count": 0, "sessions": set(), "suggestions": [], "tables": set(), "best": None}
        )
        entry["count"] += 1
        entry["sessions"].add(call.session_id)
        entry["tables"] |= query_tables(call.sql, call.dialect)
        for s in suggestions:
            if s not in entry["suggestions"]:
                entry["suggestions"].append(s)
    for wrong, entry in out.items():
        ranked = sorted(
            entry["suggestions"], key=lambda s: -difflib.SequenceMatcher(None, wrong, s.lower()).ratio()
        )
        entry["closest"] = ranked
        if ranked and difflib.SequenceMatcher(None, wrong, ranked[0].lower()).ratio() >= 0.6:
            entry["best"] = ranked[0]
    return sorted(out.items(), key=lambda item: -item[1]["count"])


def _epoch_mismatches(failed: list[ToolCall], cache: SchemaCache) -> Counter:
    # Only real dataset columns; Spark also reports CTE aliases such as `min`.
    known = {c.name.lower() for d in cache.datasets.values() for c in d.columns}
    found: Counter = Counter()
    for call in failed:
        if call.error_class != TYPE_MISMATCH:
            continue
        match = _MISMATCH.search(call.output)
        if not match:
            continue
        expression, left, right = match.groups()
        types = {left, right}
        if not types & {"DATE", "TIMESTAMP"} or not types & {"INT", "BIGINT", "LONG", "DOUBLE"}:
            continue
        if not re.search(r"\b1000\b|86400000|\b\d{12,}\b", expression):
            continue
        column = re.match(r"\(?\s*([A-Za-z_]\w*)", expression)
        if column and column.group(1).lower() in known:
            table = next(iter(sorted(query_tables(call.sql, call.dialect))), "?")
            found[(column.group(1), "DATE" if "DATE" in types else "TIMESTAMP", table)] += 1
    return found


def _epoch_rendered(calls: list[ToolCall], cache: SchemaCache) -> int:
    """Successful results whose DATE/TIMESTAMP columns show up as 13-digit numbers."""
    date_columns = {
        c.name.lower() for d in cache.datasets.values() for c in d.columns if c.type.upper().startswith(("DATE", "TIMESTAMP"))
    }
    hits = 0
    for call in calls:
        if call.kind != QUERY or call.status != OK or not call.header:
            continue
        idx = [i for i, name in enumerate(call.header) if name.lower() in date_columns]
        if not idx:
            continue
        rows = [line for line in call.output.splitlines() if line.startswith("|")][2:4]
        for row in rows:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            if any(i < len(cells) and re.fullmatch(r"-?\d{12,14}", cells[i]) for i in idx):
                hits += 1
                break
    return hits


# --------------------------------------------------------------------------- export


def replay_records(calls: list[ToolCall]) -> list[QueryAuditRecord]:
    """Recovered retry chains as `eval replay` input: the question and the SQL that worked.

    Each record is an ``ask`` audit record whose ``rewritten_sql`` is the query
    that finally succeeded, so ``querypilot eval replay`` turns it into a case
    with that query as gold SQL. The failed attempts ride along in ``details``.
    """
    _, chains = retry_chains(calls)
    records = []
    for failed, recovered in chains:
        if recovered is None or not recovered.sql:
            continue
        question = failed[0].question or recovered.question
        if not question:
            continue
        records.append(
            QueryAuditRecord(
                timestamp=failed[0].started_at or datetime.now(UTC),
                operation="ask",
                question=question,
                sql=recovered.sql,
                rewritten_sql=recovered.sql,
                valid=True,
                executed=True,
                row_count=recovered.row_count if recovered.row_count else 1,
                session_id=recovered.session_id,
                app_name=f"trace-{failed[0].error_class}",
                tool_name=recovered.tool,
                tool_call_id=recovered.call_id,
                details={
                    "failed_sql": [c.sql for c in failed],
                    "failure_classes": [c.error_class for c in failed],
                    "dialect": recovered.dialect,
                },
            )
        )
    return records


# --------------------------------------------------------------------------- rendering


def render_markdown(report: DoctorReport, *, title: str = "QueryPilot trace doctor") -> str:
    r = report
    lines = [f"# {title}", ""]
    period = f"{r.first_call} to {r.last_call}" if r.first_call else "n/a"
    lines += [
        f"Tools: {', '.join(f'`{t}`' for t in r.tools) or 'none'}. Period: {period}.",
        "",
        f"- **{r.calls:,}** query calls in **{r.sessions}** sessions ({r.threads} transcripts, including subagents)",
        f"- **{r.failed:,}** failed: **{r.failure_rate:.1%}** of the {r.ran:,} that ran"
        + (f"; {r.denied} denied by the user or a permission rule" if r.denied else "")
        + (f"; {r.blocked} blocked by the QueryPilot guard" if r.blocked else "")
        + (f"; {r.no_result} never returned" if r.no_result else ""),
    ]
    lat = r.latency
    if lat.p50 is not None:
        lines.append(
            f"- Latency p50 **{lat.p50:.1f}s**, p90 **{lat.p90:.1f}s**, p99 {lat.p99:.1f}s, max {lat.max:.0f}s; "
            f"**{lat.stalls}** calls over {lat.stall_seconds:.0f}s ({lat.over_300s} over 5 min)"
        )
    lines += ["", "## Failures by class", "", "| Class | Calls | % of calls | % of failures | Sessions | Fix first in |",
              "|---|---:|---:|---:|---:|---|"]
    for row in r.classes:
        lines.append(
            f"| {row.label} | {row.count} | {row.share_of_calls:.1%} | {row.share_of_failures:.0%} | "
            f"{row.sessions} | {row.fix} |"
        )
    c = r.chains
    lines += [
        "",
        "## Retry chains",
        "",
        ("A chain starts at a failed query and follows the next queries on the same datasets in the same "
        "transcript until one succeeds (recovered) or the agent moves on (abandoned)."),
        "",
        f"- {c.chains} chains, {c.failed_attempts} failed attempts; {c.recovered} recovered, {c.abandoned} abandoned",
        "- Failed attempts per chain: "
        + ", ".join(f"{k}: {v}" for k, v in c.attempts_histogram.items())
        + f" (max {c.max_attempts})",
        f"- Time spent on failed attempts: {c.wasted_seconds / 60:.0f} min"
        + (f"; median time from first failure to recovery {c.recovery_seconds_p50:.0f}s" if c.recovery_seconds_p50 else ""),
        f"- Agent fetched a schema mid-chain in {c.schema_fetched_in_chain} chains",
        "- First failure class: " + ", ".join(f"{CLASS_LABELS.get(k, k)} {v}" for k, v in c.by_first_class.items()),
        f"- {c.duplicate_successes} successful queries were exact repeats of an earlier success in the session",
    ]
    lines += ["", "## Latency", "", "| Outcome | p50 (s) |", "|---|---:|"]
    order = ["ok", *(row.error_class for row in r.classes)]
    for name, value in sorted(lat.by_class_p50.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99):
        lines.append(f"| {CLASS_LABELS.get(name, 'Succeeded' if name == 'ok' else name)} | {value} |")
    g = r.guard
    lines += [
        "",
        "## Guard replay",
        "",
        (f"The schema cache built from this history holds {g.cached_datasets} datasets. The guard could check "
        f"columns for {g.checked:,} queries (every dataset cached at the time)."),
        "",
        "- Failures it would have stopped before execution: "
        + (", ".join(f"{CLASS_LABELS.get(k, k)} {v}" for k, v in g.caught.items()) or "none")
        + ". Outside the column and type classes, these queries failed for another reason first but "
        "also used a column the dataset lacks or did date arithmetic on a DATE column.",
        (f"- Oversized results the row cap or the wide `SELECT *` rule would have stopped: {g.size_caught} "
        f"({g.limit_missing_in_too_large} lacked a row cap, {g.wide_star_in_too_large} read every column of a "
        f"cached wide dataset). These rules would also have sent {g.size_blocks_of_successes} successful "
        "queries back once for a LIMIT or named columns."),
        (f"- Successful queries it would have blocked by mistake: {g.false_blocks} of {g.successes_checked:,} "
        "checked. These are mostly datasets rebuilt with new columns after their schema was cached; "
        "live, the agent resends the query once and the hook drops the stale entry."),
    ]
    if g.caught_examples:
        lines += ["", "Sample guard replies (redacted):", ""]
        lines += [f"- {e}" for e in g.caught_examples[:3]]
    lines += ["", "## Findings", ""]
    for i, f in enumerate(r.findings, 1):
        lines += [f"### {i}. [{f.fix} fix] {f.title} ({f.impact} calls)", "", f.detail, ""]
        lines += [f"- {e}" for e in f.examples]
        if f.examples:
            lines.append("")
        lines += [f"**Fix:** {f.suggestion}", ""]
    if r.hints:
        lines += ["## Suggested prompt hints", "", "```yaml", "hints:"]
        lines += [f"  - {json.dumps(h)}" for h in r.hints]
        lines += ["```", ""]
    if r.descriptions:
        lines += ["## Suggested column descriptions", "", "```yaml", "descriptions:"]
        lines += [f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(r.descriptions.items())]
        lines += ["```", ""]
    return "\n".join(lines).rstrip() + "\n"


def render_terminal(report: DoctorReport) -> str:
    r = report
    lat = r.latency
    out = [
        f"{r.calls:,} query calls, {r.sessions} sessions; {r.failed:,} failed ({r.failure_rate:.1%})",
    ]
    if lat.p50 is not None:
        out.append(f"latency p50 {lat.p50:.1f}s  p90 {lat.p90:.1f}s  >{lat.stall_seconds:.0f}s: {lat.stalls}")
    out.append("")
    for row in r.classes:
        out.append(f"  {row.count:>5}  {row.share_of_calls:>6.1%}  {row.label:<44} fix: {row.fix}")
    c = r.chains
    out += [
        "",
        (f"retry chains: {c.chains} ({c.recovered} recovered, {c.abandoned} abandoned), "
        f"{c.failed_attempts} failed attempts, {c.wasted_seconds / 60:.0f} min"),
        (f"guard replay: would stop {sum(r.guard.caught.values()) + r.guard.size_caught} failures "
        f"({sum(r.guard.caught.values())} by schema, {r.guard.size_caught} by row cap or SELECT *), "
        f"{r.guard.false_blocks} false schema blocks of {r.guard.successes_checked:,} checked successes, "
        f"{r.guard.size_blocks_of_successes} successes sent back for a LIMIT or named columns"),
        "",
        "findings:",
    ]
    out += [f"  [{f.fix}] {f.title} ({f.impact})" for f in r.findings]
    return "\n".join(out)
