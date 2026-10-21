"""Read agent query-tool calls out of Claude Code transcripts and audit logs.

Claude Code writes one JSONL transcript per session under
``~/.claude/projects/<project>/``. Each assistant ``tool_use`` block is answered
by a ``tool_result`` block with the same id in a later user record; pairing
them gives the SQL, the outcome and the latency (the gap between the two
records' timestamps, which includes any permission prompt). Resumed sessions
copy earlier history into a new file, so calls are de-duplicated by id.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from querypilot.audit.types import QueryAuditRecord
from querypilot.trace.classify import (
    BLOCKED,
    DENIED,
    NO_RESULT,
    OK,
    classify_output,
    markdown_header,
)
from querypilot.trace.schema_cache import CachedDataset, parse_foundry_schema
from querypilot.trace.tools import ToolRegistry

QUERY = "query"
SCHEMA = "schema"
TOOL_CALL_OPERATION = "tool_call"

_KEPT_OUTPUT = 2000
_KEPT_QUESTION = 500


@dataclass
class ToolCall:
    call_id: str
    tool: str
    kind: str
    session_id: str | None = None
    # Transcript file (one per session or subagent); retry chains never cross it.
    thread: str | None = None
    sequence: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    sql: str | None = None
    dialect: str = "spark"
    params: dict = field(default_factory=dict)
    # Result or error text, truncated; held in memory only, never written by import.
    output: str = ""
    result_chars: int | None = None
    is_error: bool = False
    status: str = NO_RESULT
    error_class: str | None = None
    row_count: int | None = None
    spilled: bool = False
    header: list[str] | None = None
    dataset: CachedDataset | None = None
    question: str | None = None

    @property
    def latency_s(self) -> float | None:
        if self.duration_ms is not None:
            return self.duration_ms / 1000
        if self.started_at and self.ended_at:
            return max((self.ended_at - self.started_at).total_seconds(), 0.0)
        return None

    @property
    def ran(self) -> bool:
        return self.status not in (DENIED, BLOCKED)

    def apply_result(self, text: str, *, is_error: bool, ended_at: datetime | None) -> None:
        self.ended_at = ended_at
        self.is_error = is_error
        self.result_chars = len(text)
        self.output = text[:_KEPT_OUTPUT]
        if self.kind == SCHEMA:
            self.dataset = None if is_error else parse_foundry_schema(text)
            self.status = OK if self.dataset else ("error" if is_error else OK)
            return
        outcome = classify_output(text, is_error=is_error)
        self.status, self.error_class = outcome.status, outcome.error_class
        self.row_count, self.spilled = outcome.row_count, outcome.spilled
        if outcome.status == OK:
            self.header = markdown_header(text[:20000])


def iter_transcript_files(paths: Iterable[str | Path]) -> Iterator[Path]:
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            yield from sorted(path.rglob("*.jsonl"))
        elif path.is_file():
            yield path


def is_audit_log(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
        return '"audit_id"' in first and '"operation"' in first
    except OSError:
        return False


def load_calls(paths: Iterable[str | Path], registry: ToolRegistry | None = None) -> list[ToolCall]:
    """Tool calls from transcripts and/or QueryPilot audit logs, de-duplicated by id."""
    registry = registry or ToolRegistry()
    calls: dict[str, ToolCall] = {}
    for path in iter_transcript_files(paths):
        found = read_audit_log(path, registry) if is_audit_log(path) else read_transcript(path, registry)
        for call in found:
            current = calls.get(call.call_id)
            # Prefer the copy that saw the result; hooks measure latency exactly.
            if current is None or (current.status == NO_RESULT and call.status != NO_RESULT):
                calls[call.call_id] = call
            elif call.duration_ms is not None and current.duration_ms is None:
                current.duration_ms = call.duration_ms
    ordered = sorted(calls.values(), key=lambda c: (c.started_at is None, c.started_at or datetime.min, c.sequence))
    return ordered


def read_transcript(path: Path, registry: ToolRegistry) -> list[ToolCall]:
    pending: dict[str, ToolCall] = {}
    out: list[ToolCall] = []
    question: str | None = None
    sequence = 0
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return []
    with handle:
        for line in handle:
            if '"tool_use"' not in line and '"tool_result"' not in line and '"user"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            timestamp = _parse_time(record.get("timestamp"))
            # Skill bodies, reminders and compaction summaries arrive as user records too.
            injected = bool(record.get("isMeta") or record.get("isCompactSummary"))
            if message.get("role") == "user" and isinstance(content, str):
                question = (None if injected else _prompt_text(content)) or question
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and message.get("role") == "user" and not injected:
                    question = _prompt_text(block.get("text")) or question
                elif kind == "tool_use":
                    call = _call_from_tool_use(block, registry)
                    if call is None:
                        continue
                    sequence += 1
                    call.sequence = sequence
                    call.session_id = record.get("sessionId") or path.stem
                    call.thread = str(path)
                    call.started_at = timestamp
                    call.question = question
                    pending[call.call_id] = call
                    out.append(call)
                elif kind == "tool_result":
                    call = pending.pop(block.get("tool_use_id"), None)
                    if call is not None:
                        call.apply_result(
                            result_text(block.get("content")),
                            is_error=bool(block.get("is_error")),
                            ended_at=timestamp,
                        )
    return out


def read_audit_log(path: Path, registry: ToolRegistry) -> list[ToolCall]:
    out = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = QueryAuditRecord.model_validate_json(line)
            except ValueError:
                continue
            if record.operation == TOOL_CALL_OPERATION and record.tool_name and record.tool_call_id:
                out.append(call_from_record(record, registry))
    return out


def _call_from_tool_use(block: dict, registry: ToolRegistry) -> ToolCall | None:
    name = block.get("name") or ""
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    spec = registry.query_spec(name)
    if spec is not None:
        sql = tool_input.get(spec.sql_field)
        return ToolCall(
            call_id=str(block.get("id")),
            tool=name,
            kind=QUERY,
            sql=sql if isinstance(sql, str) else None,
            dialect=spec.dialect,
            params={k: v for k, v in tool_input.items() if k != spec.sql_field and small_value(v)},
        )
    if registry.schema_spec(name) is not None:
        return ToolCall(
            call_id=str(block.get("id")),
            tool=name,
            kind=SCHEMA,
            params={k: v for k, v in tool_input.items() if small_value(v)},
        )
    return None


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text"
        )
    if isinstance(content, dict):
        return json.dumps(content)
    return "" if content is None else str(content)


def to_audit_record(call: ToolCall) -> QueryAuditRecord:
    """The audit record `trace import` writes: SQL, outcome and timing, never result rows."""
    error = None
    if call.status not in (OK, NO_RESULT):
        error = call.output[:500]
    return QueryAuditRecord(
        timestamp=call.started_at or datetime.now().astimezone(),
        operation=TOOL_CALL_OPERATION,
        question=call.question,
        sql=call.sql,
        executed=call.status == OK,
        row_count=call.row_count,
        execution_time_ms=round(call.latency_s * 1000) if call.latency_s is not None else None,
        error=error,
        session_id=call.session_id,
        tool_name=call.tool,
        tool_call_id=call.call_id,
        details={
            "kind": call.kind,
            "status": call.status,
            "error_class": call.error_class,
            "thread": call.thread,
            "sequence": call.sequence,
            "ended_at": call.ended_at.isoformat() if call.ended_at else None,
            "result_chars": call.result_chars,
            "spilled": call.spilled,
            "params": call.params,
            "dialect": call.dialect,
            "header": call.header,
            "dataset": _dataset_payload(call.dataset),
        },
    )


def call_from_record(record: QueryAuditRecord, registry: ToolRegistry) -> ToolCall:
    details = record.details or {}
    spec = registry.query_spec(record.tool_name or "")
    dataset = None
    if details.get("dataset"):
        from querypilot.trace.schema_cache import CachedColumn

        raw = details["dataset"]
        dataset = CachedDataset(
            key=raw["key"], path=raw.get("path"), columns=[CachedColumn(**c) for c in raw.get("columns", [])]
        )
    call = ToolCall(
        call_id=record.tool_call_id or record.audit_id,
        tool=record.tool_name or "",
        kind=details.get("kind") or (QUERY if spec else SCHEMA),
        session_id=record.session_id,
        thread=details.get("thread") or record.session_id,
        sequence=int(details.get("sequence") or 0),
        started_at=record.timestamp,
        ended_at=_parse_time(details.get("ended_at")),
        duration_ms=details.get("duration_ms"),
        sql=record.sql,
        dialect=details.get("dialect") or (spec.dialect if spec else "spark"),
        params=details.get("params") or {},
        output=record.error or "",
        result_chars=details.get("result_chars"),
        status=details.get("status") or (OK if record.executed else "error"),
        error_class=details.get("error_class"),
        row_count=record.row_count,
        spilled=bool(details.get("spilled")),
        header=details.get("header"),
        dataset=dataset,
        question=record.question,
    )
    if call.duration_ms is None and record.execution_time_ms is not None and call.ended_at is None:
        call.duration_ms = record.execution_time_ms
    return call


def _dataset_payload(dataset: CachedDataset | None) -> dict | None:
    if dataset is None:
        return None
    return {
        "key": dataset.key,
        "path": dataset.path,
        "columns": [{"name": c.name, "type": c.type, "description": c.description} for c in dataset.columns],
    }


def _prompt_text(text) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    # Slash-command wrappers, hook output and system reminders are not the user's question.
    if not text or text.startswith(("<", "[Request interrupted")):
        return None
    return text[:_KEPT_QUESTION]


def small_value(value) -> bool:
    return isinstance(value, (int, float, bool)) or (isinstance(value, str) and len(value) <= 200)


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
