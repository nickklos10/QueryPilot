# Design note: `querypilot trace` and `querypilot hooks`

Status: first version. Claude Code transcripts and hooks; Palantir Foundry's SQL tool is mapped by default, and any other MCP or connector query tool can be mapped by name.

## Why

Agents often run SQL through a tool that QueryPilot cannot sit in front of. Palantir Foundry's MCP server (`mcp__palantir-mcp__run_sql_query_on_foundry_dataset`) is one: Foundry does not speak the Postgres protocol, so `querypilot proxy` cannot intercept it. What QueryPilot can see is the agent's side of the conversation: Claude Code writes every `tool_use` and `tool_result` to a JSONL transcript, and its hooks run before and after each tool call. `trace` reads the first, `hooks` uses the second, and `trace doctor` says where each recurring problem is best fixed:

- **tool**: QueryPilot's guard, or the tool itself (validate before running, cap rows, retry transport errors);
- **prompt**: a hint the agent should get up front (the dialect's rules, the real name of a column it keeps guessing);
- **data**: the dataset should describe or model itself better (a column description, a view with a proper DATE column).

## Commands

```bash
# Read past sessions (default ~/.claude/projects) into the audit log and schema cache
querypilot trace import

# Diagnose: terminal summary, or a shareable markdown/JSON report (redacted by default)
querypilot trace doctor
querypilot trace doctor ~/.claude/projects --output report.md

# Export recovered retry chains (failed query -> the SQL that worked) as an eval replay suite
querypilot trace doctor --export-replay chains.jsonl
querypilot eval replay --audit-jsonl chains.jsonl --fixture-db sqlite:///mirror.db --output suite.yaml

# Log every query tool call live (PostToolUse + PostToolUseFailure)
querypilot hooks install                       # ~/.claude/settings.json; --scope project|local, or --settings PATH
querypilot hooks install --guard --max-rows 200 --require-schema
querypilot hooks uninstall
```

Other tools are mapped with `--tool NAME=SQL_FIELD[:DIALECT[:ROW_LIMIT_FIELD]]`, where `NAME` may be a glob, for example `--tool 'mcp__warehouse__*=query:snowflake'`. Foundry's SQL tool (`sqlQuery`, Spark SQL, `rowLimit`) and its schema tool are always included.

## What gets recorded

`trace import` and the hooks write `QueryAuditRecord`s (operation `tool_call`) to `~/.querypilot/trace/audit.jsonl` (`QUERYPILOT_HOME` moves it). Each record holds the SQL, the outcome and failure class, the latency, the session and transcript, the result size in characters and rows, and the result's column header. Result rows are never written. An error message is kept (first 500 characters) because the failure class and the "did you mean" suggestions come from it. `trace import` also keeps the user's last prompt before the call (first 500 characters) as `question`, which is what the replay export needs; all of this stays on the local machine.

Calls are paired by `tool_use_id`, so a call seen in both a transcript and the live log, or in a resumed session's copied history, counts once. Latency from a transcript is the gap between the `tool_use` and `tool_result` records, which includes any permission prompt; from the hooks it is Claude Code's `duration_ms`.

## Failure classes

Foundry reports query failures as ordinary text (`Sql Query Failed ... [UNRESOLVED_COLUMN...]`), not as tool errors, so `trace/classify.py` reads the text. The classes are: unresolved column, unknown table/dataset, data type mismatch, result too large for the context window, timeout, upstream/network, syntax, other SQL analysis error, and invalid tool call. Calls the user or a permission rule refused are counted as denied, not as failures, and calls the guard blocked are counted as blocked.

Two Foundry details matter. The MCP server appends a "403 Forbidden ... the dataset RID is incorrect" suggestion to every HTTP failure, so the HTTP status decides first: 5xx is upstream, 403/404 is unknown dataset (Foundry answers 403 for both a wrong RID and missing access). Second, a bare `Failed to fetch 400` followed by a stack trace means the tool dropped Foundry's error body, so it counts as upstream: the agent cannot act on it.

## The guard

`querypilot hooks install --guard` adds a PreToolUse hook. For each query it:

1. parses the SQL in the tool's dialect (Spark for Foundry); if sqlglot cannot parse it, the call is allowed, because the engine's parser is the authority;
2. looks up every referenced dataset (RID or path) in the schema cache, which is filled from `get_foundry_dataset_schema` results and from successful `SELECT *` queries on a single dataset;
3. when all of them are cached, runs `SQLValidator` against those schemas and denies unknown or ambiguous columns, with `repair_hints`' "did you mean" text, and denies arithmetic that treats a DATE/TIMESTAMP column as epoch milliseconds (`Report_Date / 1000`, `Report_Date >= 1767225600000`);
4. with `--max-rows N`, denies queries that have neither `LIMIT <= N`, a tool row limit `<= N`, nor an aggregate-only projection;
5. with `--require-schema`, denies queries on datasets the agent has not fetched a schema for, which turns a wrong RID into a cheap schema-call failure.

A dataset that is not cached is never judged. Datasets get rebuilt with new columns, so a cached schema can go stale. When the guard denies a query for its columns, the message tells the agent it may resend the identical query, and the hook allows that resend. If the query then succeeds, the PostToolUse hook drops the stale cache entry. The denial costs one turn, not the task.

With `--guard`, the PostToolUse hook also adds context when a result shows a DATE/TIMESTAMP column as a 13-digit number. Foundry renders dates as epoch milliseconds, and in the history below that is where the agent learned to write `Report_Date / 1000`.

Hooks never fail a tool call: every exception is caught and logged to `hook-errors.log` next to the audit log, and the hook prints nothing.

## `trace doctor`

The doctor reports:

- the failure rate overall and by class, with the layer to fix first;
- **retry chains**: a failed query, then the following queries on the same datasets in the same transcript, up to the first success (recovered) or until the agent moves to other datasets (abandoned). It reports attempts per chain, time spent on failed attempts, chains where the agent fetched a schema only after failing, and exact repeats of queries that had already succeeded;
- **latency**: p50/p90/p99, stalls (`--stall-seconds`, default 60), and p50 by outcome;
- **guard replay**: the history replayed in time order, building the schema cache the way the hooks would, to count the failures the guard would have stopped and the successful queries it would have blocked;
- **findings**, each tagged tool, prompt or data, plus ready-to-paste `hints:` and `descriptions:` blocks for `querypilot.yaml` or the agent's instructions.

Markdown and JSON reports are redacted by default: dataset RIDs and paths become `dataset_A`, `dataset_B`, ..., string literals become `'?'`, and result rows are never shown. Column names are kept. `--no-redact` shows dataset names, for local use.

## First run: the Foundry history

[trace-doctor-foundry-2026-10-21.md](trace-doctor-foundry-2026-10-21.md) is `trace doctor` over three months of Claude Code sessions that used Foundry. A quick scan done before this work estimated the same picture with looser regexes; the doctor's numbers line up:

| | Quick scan | `trace doctor` | Why they differ |
|---|---:|---:|---|
| Foundry SQL calls | 5,157 | 5,163 | sessions kept running between the two counts |
| Sessions | 228 | 71 sessions, 224 transcripts | the scan counted transcript files, which include subagents |
| Failed | 12% | 11.8% (609) | |
| Unresolved column | 231 | 191 | the scan's `cannot resolve` pattern also matched type-mismatch errors |
| Result too large | 165 | 165 | |
| Unknown table | 80 | 57 | 5xx responses carrying Foundry's "403 ... RID is incorrect" suggestion are upstream errors |
| Data type mismatch | 34 | 47 | adds `CAST_INVALID_INPUT` and unparseable values |
| Network + Foundry internal | 42 + 21 | 107 upstream | adds 37 bare `Failed to fetch 400` errors with no body |
| Latency p50 / p90 | 3.8s / 11.7s | 3.8s / 11.6s | |
| Calls over 60s | 84 | 84 | |

Replaying the guard over that history, it would have stopped 98 of the 191 unresolved-column failures and 24 of the 47 type mismatches before they reached Foundry. It would have blocked 38 of 3,099 successful queries it could check, mostly on datasets rebuilt with new columns after their schema was cached; with the resend rule each of those costs one turn.

## Known gaps

- Only Claude Code's transcript format is read. Other agents need their own reader that produces `ToolCall`s.
- The schema-tool parser understands Foundry's resource tree only. Other schema tools need a parser (`SchemaToolSpec.format`).
- Retry chains are a heuristic: they link queries that share a dataset in one transcript, so two unrelated questions about one dataset can join a chain.
- The guard cannot see Spark struct fields or backticked names that contain dots, so it skips those queries instead of guessing.
