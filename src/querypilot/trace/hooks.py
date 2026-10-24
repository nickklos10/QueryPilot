"""Claude Code hooks that log query-tool calls live and, opt-in, guard them.

``querypilot hooks install`` adds PostToolUse and PostToolUseFailure hooks (and
a PreToolUse hook with ``--guard``) to a Claude Code settings file. Each hook
runs ``python -m querypilot hooks run <event>``, which reads the hook payload
on stdin and:

- **post**: appends one audit record per query call (SQL, outcome, failure
  class, latency, result size; never result rows) to the JSONL audit log, and
  adds schema-tool results to the schema cache;
- **pre** (guard): validates the SQL against the schema cache and denies the
  call with "did you mean" feedback, or when it has no row cap. A query that
  was denied for its columns goes through if the agent sends it again
  unchanged, so a stale cache entry costs one turn, not the task.

A hook must never break the tool call: every error is swallowed (and logged to
``hook-errors.log`` next to the audit log), and the hook prints nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from querypilot.audit.sinks import JSONLAuditSink
from querypilot.trace.classify import BLOCKED, OK, markdown_header
from querypilot.trace.schema_cache import SchemaCache, querypilot_home
from querypilot.trace.tools import ToolRegistry, parse_tool_spec
from querypilot.trace.transcript import (
    QUERY,
    SCHEMA,
    ToolCall,
    result_text,
    small_value,
    to_audit_record,
)

HOOK_MARKER = "querypilot hooks run"
SCOPES = ("user", "project", "local")
_STATE_TTL_SECONDS = 6 * 3600


def default_audit_log() -> Path:
    return querypilot_home() / "trace" / "audit.jsonl"


def settings_path(scope: str, project_dir: str | Path | None = None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    base = Path(project_dir or os.getcwd())
    return base / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")


DEFAULT_MAX_STAR_COLUMNS = 20


@dataclass
class HookOptions:
    audit_log: Path = field(default_factory=default_audit_log)
    cache_path: Path | None = None
    guard: bool = False
    max_rows: int | None = None
    require_schema: bool = False
    # With guard: deny SELECT * on cached datasets wider than this; 0 turns it off.
    max_star_columns: int = DEFAULT_MAX_STAR_COLUMNS
    tools: list[str] = field(default_factory=list)

    @property
    def registry(self) -> ToolRegistry:
        return ToolRegistry.from_args(self.tools)

    def cli_args(self) -> list[str]:
        args = ["--audit-log", str(self.audit_log)]
        if self.cache_path:
            args += ["--cache", str(self.cache_path)]
        if self.guard:
            args.append("--guard")
        if self.max_rows is not None:
            args += ["--max-rows", str(self.max_rows)]
        if self.require_schema:
            args.append("--require-schema")
        if self.max_star_columns != DEFAULT_MAX_STAR_COLUMNS:
            args += ["--max-star-columns", str(self.max_star_columns)]
        for tool in self.tools:
            args += ["--tool", tool]
        return args

    @property
    def state_path(self) -> Path:
        return self.audit_log.parent / "hook-state.json"


# --------------------------------------------------------------------------- install


def hook_command(event: str, options: HookOptions, python: str | None = None) -> str:
    parts = [python or sys.executable, "-m", "querypilot", "hooks", "run", event, *options.cli_args()]
    return " ".join(shlex.quote(part) for part in parts)


def tool_matcher(names: list[str]) -> str:
    """Claude Code treats a matcher of [A-Za-z0-9_|-] as an exact list, anything else as a regex."""
    if all(re.fullmatch(r"[\w-]+", name) for name in names):
        return "|".join(names)
    alternatives = [re.sub(r"([.+?^${}()\[\]\\])", r"\\\1", name).replace("*", ".*") for name in names]
    return "^(?:" + "|".join(alternatives) + ")$"


def install_hooks(path: Path, options: HookOptions, *, python: str | None = None) -> dict:
    settings = _read_settings(path)
    hooks = _remove_ours(settings.get("hooks") or {})
    registry = options.registry
    all_tools = tool_matcher(registry.tool_names())
    query_tools = tool_matcher([spec.name for spec in registry.query_tools])
    events = [("PostToolUse", "post", all_tools), ("PostToolUseFailure", "post-failure", all_tools)]
    if options.guard:
        events.insert(0, ("PreToolUse", "pre", query_tools))
    for event, name, matcher in events:
        hooks.setdefault(event, []).append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": hook_command(name, options, python), "timeout": 30}],
            }
        )
    settings["hooks"] = hooks
    _write_settings(path, settings)
    return settings


def uninstall_hooks(path: Path) -> int:
    settings = _read_settings(path)
    before = json.dumps(settings.get("hooks") or {})
    hooks = _remove_ours(settings.get("hooks") or {})
    removed = before.count(HOOK_MARKER) - json.dumps(hooks).count(HOOK_MARKER)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    if removed:
        _write_settings(path, settings)
    return removed


def _remove_ours(hooks: dict) -> dict:
    cleaned: dict = {}
    for event, groups in hooks.items():
        kept_groups = []
        for group in groups if isinstance(groups, list) else []:
            entries = [h for h in group.get("hooks", []) if HOOK_MARKER not in str(h.get("command", ""))]
            if entries:
                kept_groups.append({**group, "hooks": entries})
        if kept_groups:
            cleaned[event] = kept_groups
    return cleaned


def _read_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError(f"{path} does not hold a JSON object")
    return data


def _write_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- run


def run_hook(event: str, payload: dict, options: HookOptions) -> dict | None:
    """Handle one hook event; return the JSON to print, or None for no output."""
    tool_name = str(payload.get("tool_name") or "")
    registry = options.registry
    spec = registry.query_spec(tool_name)
    is_schema = spec is None and registry.schema_spec(tool_name) is not None
    if spec is None and not is_schema:
        return None
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    call_id = str(payload.get("tool_use_id") or "")
    state = _State.load(options.state_path)

    if event == "pre":
        if spec is None or not options.guard:
            return None
        state.starts[call_id] = time.time()
        output = _pre(payload, tool_input, spec, options, state)
        state.save(options.state_path)
        return output

    call = ToolCall(
        call_id=call_id or f"hook-{time.time_ns()}",
        tool=tool_name,
        kind=QUERY if spec else SCHEMA,
        session_id=payload.get("session_id"),
        thread=_thread(payload),
        sql=tool_input.get(spec.sql_field) if spec and isinstance(tool_input.get(spec.sql_field), str) else None,
        dialect=spec.dialect if spec else "spark",
        params={
            k: v for k, v in tool_input.items() if (spec is None or k != spec.sql_field) and small_value(v)
        },
    )
    now = datetime.now(UTC)
    started = state.starts.pop(call_id, None)
    duration = payload.get("duration_ms")
    if isinstance(duration, int | float):
        call.duration_ms = int(duration)
    elif started:
        call.duration_ms = round((time.time() - started) * 1000)
    call.started_at = datetime.fromtimestamp(started, UTC) if started else now
    if event == "post-failure":
        text, is_error = str(payload.get("error") or "tool call failed"), True
    else:
        response = payload.get("tool_response")
        text = result_text(response.get("content") if isinstance(response, dict) and "content" in response else response)
        is_error = bool(isinstance(response, dict) and response.get("isError"))
    call.apply_result(text, is_error=is_error, ended_at=now)

    record = to_audit_record(call)
    record.details["source"] = "hook"
    record.details["duration_ms"] = call.duration_ms
    JSONLAuditSink(options.audit_log).write(record)

    output = _learn(call, tool_input, text, options) if (options.guard or is_schema) else None
    if state.dirty:
        state.save(options.state_path)
    return output


def _pre(payload: dict, tool_input: dict, spec, options: HookOptions, state: _State) -> dict | None:
    from querypilot.trace.guard import guard_sql

    sql = tool_input.get(spec.sql_field)
    if not isinstance(sql, str) or not sql.strip():
        return None
    row_limit = tool_input.get(spec.row_limit_field) if spec.row_limit_field else None
    decision = guard_sql(
        sql,
        SchemaCache.load(options.cache_path),
        dialect=spec.dialect,
        max_rows=options.max_rows,
        tool_row_limit=int(row_limit) if isinstance(row_limit, int | float | str) and str(row_limit).isdigit() else None,
        require_schema=options.require_schema,
        max_star_columns=options.max_star_columns or None,
        schema_tool_hint="the dataset schema tool",
    )
    if not decision.denied:
        return None
    session = str(payload.get("session_id") or "")
    fingerprint = hashlib.sha256(f"{session}\n{sql.strip()}".encode()).hexdigest()[:16]
    # The agent resent a query denied for its columns: trust it, the cache may be stale.
    if decision.rule == "schema" and fingerprint in state.denied:
        state.denied.pop(fingerprint, None)
        return None
    state.denied[fingerprint] = time.time()
    reason = decision.reason or "Blocked by the QueryPilot guard."
    if decision.rule == "schema":
        reason += " If you are sure the query is right (the dataset may have changed), send it again unchanged."

    call = ToolCall(
        call_id=str(payload.get("tool_use_id") or f"hook-{time.time_ns()}"),
        tool=str(payload.get("tool_name")),
        kind=QUERY,
        session_id=payload.get("session_id"),
        thread=_thread(payload),
        started_at=datetime.now(UTC),
        sql=sql,
        dialect=spec.dialect,
        output=reason,
        status=BLOCKED,
    )
    record = to_audit_record(call)
    record.valid = False
    record.details.update({"source": "hook", "guard_rule": decision.rule})
    JSONLAuditSink(options.audit_log).write(record)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _learn(call: ToolCall, tool_input: dict, text: str, options: HookOptions) -> dict | None:
    from querypilot.trace.guard import guard_sql

    cache = SchemaCache.load(options.cache_path)
    changed = False
    if call.kind == SCHEMA:
        changed = cache.learn_from_schema_result(text, tool_input) is not None
    elif call.status == OK and call.sql:
        # A query that succeeded although the cache says it should fail proves the entry stale.
        decision = guard_sql(call.sql, cache, dialect=call.dialect)
        if decision.denied and decision.rule == "schema":
            changed = bool(cache.evict(decision.tables))
        header = markdown_header(text[:20000])
        if header and cache.learn_from_select_star(call.sql, header, call.dialect):
            changed = True
    if changed:
        cache.save(options.cache_path)
    note = _epoch_note(call, text, cache) if call.kind == QUERY and call.status == OK else None
    if note:
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}
    return None


def _epoch_note(call: ToolCall, text: str, cache: SchemaCache) -> str | None:
    """Foundry renders DATE/TIMESTAMP values as epoch ms; say so before the agent does arithmetic on them."""
    from querypilot.trace.doctor import query_tables

    header = call.header or []
    if not header:
        return None
    types = {}
    for ref in query_tables(call.sql, call.dialect):
        dataset = cache.lookup(ref)
        for column in dataset.columns if dataset else []:
            types[column.name.lower()] = column.type.upper()
    rows = [line for line in text.splitlines() if line.startswith("|")][2:5]
    flagged = []
    for i, name in enumerate(header):
        column_type = types.get(name.lower(), "")
        if not column_type.startswith(("DATE", "TIMESTAMP")):
            continue
        for row in rows:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            if i < len(cells) and re.fullmatch(r"-?\d{12,14}", cells[i]):
                flagged.append(f"{name} ({column_type})")
                break
    if not flagged:
        return None
    return (
        f"QueryPilot: {', '.join(flagged)} are date columns displayed as epoch milliseconds. "
        "In SQL they are dates: filter with DATE 'YYYY-MM-DD' literals or date functions, "
        "never divide by 1000 or compare with 13-digit numbers."
    )


def _thread(payload: dict) -> str | None:
    transcript = payload.get("transcript_path")
    agent = payload.get("agent_id")
    if agent:
        return f"{transcript}#{agent}"
    return transcript


class _State:
    """Small per-machine state: call start times and recently denied queries."""

    def __init__(self, starts: dict | None = None, denied: dict | None = None) -> None:
        self.starts: dict[str, float] = starts or {}
        self.denied: dict[str, float] = denied or {}
        self._snapshot = json.dumps([self.starts, self.denied], sort_keys=True)

    @property
    def dirty(self) -> bool:
        return json.dumps([self.starts, self.denied], sort_keys=True) != self._snapshot

    @classmethod
    def load(cls, path: Path) -> _State:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        cutoff = time.time() - _STATE_TTL_SECONDS
        fresh = lambda d: {k: v for k, v in (d or {}).items() if isinstance(v, int | float) and v > cutoff}
        return cls(fresh(raw.get("starts")), fresh(raw.get("denied")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hook-state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"starts": self.starts, "denied": self.denied}, handle)
        os.replace(tmp, path)


def main_hook(event: str, options: HookOptions, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        import logging

        logging.getLogger("sqlglot").setLevel(logging.ERROR)
        payload = json.loads(stdin.read() or "{}")
        output = run_hook(event, payload, options) if isinstance(payload, dict) else None
        if output:
            stdout.write(json.dumps(output))
    except Exception:  # noqa: BLE001 - a logging hook must never fail the tool call
        try:
            log = options.audit_log.parent / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now(UTC).isoformat()} {event}\n{traceback.format_exc()}\n")
        except OSError:
            pass
    return 0


def options_from_args(args) -> HookOptions:
    for value in args.tool or []:
        parse_tool_spec(value)  # fail fast on a malformed --tool
    return HookOptions(
        audit_log=Path(args.audit_log).expanduser() if args.audit_log else default_audit_log(),
        cache_path=Path(args.cache).expanduser() if args.cache else None,
        guard=bool(getattr(args, "guard", False)),
        max_rows=getattr(args, "max_rows", None),
        require_schema=bool(getattr(args, "require_schema", False)),
        max_star_columns=getattr(args, "max_star_columns", DEFAULT_MAX_STAR_COLUMNS),
        tools=list(args.tool or []),
    )
