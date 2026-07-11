# Changelog

All notable changes to QueryPilot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `SECURITY.md` security policy, Contributor Covenant code of conduct, issue
  templates, and a pull-request template.
- PyPI release workflow (`.github/workflows/release.yml`) using trusted
  publishing, triggered by publishing a GitHub Release.

### Changed

- CI now tests Python 3.11, 3.12, and 3.13 and runs `ruff check` as a
  separate lint job.

### Removed

- Internal planning documents from `docs/`.

## [0.1.0] - 2026-04-28

Initial public release. Foundation and eval-driven harness shipped.

### Added — Safety engine

- sqlglot-based SQL validation with single-statement and SELECT-only
  enforcement, blocked-keyword filter, table/column allowlist + blocklist,
  Cartesian-join detection, automatic `LIMIT` injection and max-row capping,
  `SELECT *` policy, query fingerprinting, and risk-level scoring.
- SQLite and PostgreSQL connectors with per-dialect read-only execution and
  query timeouts.

### Added — Audit & access control

- Structured audit trail: `QueryAuditRecord` with audit ID, original SQL,
  rewritten SQL, validation result, applied access policy, row count, and
  execution timing.
- `InMemoryAuditSink` and `JSONLAuditSink` implementations.
- Access-control policies: column allowlist + blocklist, row-filter injection
  ANDed into WHERE, post-execution column masking (redact / null / hash).

### Added — Runtimes

- FastAPI server runtime (`querypilot serve`) with `/health`, `/schema`,
  `/search-schema`, `/ask`, `/generate-sql`, `/validate-sql`, `/execute-sql`,
  `/evals/run`, and `/audit/recent` endpoints.
- MCP server runtime (`querypilot mcp`) over stdio and Streamable HTTP with
  `ask_database`, `search_schema`, `validate_sql`, and `execute_sql` tools.

### Added — Generation

- `DemoSQLGenerator` (offline, deterministic) plus `OpenAISQLGenerator` and
  `AnthropicSQLGenerator` provider implementations with a validator-driven
  repair loop.
- Tool-schema producers `as_openai_tools()` and `as_anthropic_tools()` for
  drop-in agent use.

### Added — Eval-driven harness

- `BenchmarkCase` / `BenchmarkSuite` Pydantic schema with thresholds,
  comparison config, and tags. YAML and JSON suite loaders with shared-config
  validation across multi-file suite directories.
- Result-set comparator with column- and row-order normalization,
  abs-difference float tolerance, datetime ISO normalization, NaN handling,
  and structured row diffs.
- `pipeline.run_case` per-case runner reusing existing `QueryPilot.generate_sql`,
  `validate_sql`, and `execute_sql`; per-stage `time.perf_counter` timings;
  failure classification across 12 categories.
- Cost trackers for OpenAI and Anthropic (idempotent client wrap with
  restore), `NullCostTracker`, and a `MODEL_PRICING` table.
- `run_suite` aggregator producing `SuiteReport` with pass / safety /
  correctness / repair rates, p50/p95 latency, token totals, $ estimate, tag
  rollups, failure breakdown, and threshold violations.
- Stdlib JSON writer and screenshot-quality terminal renderer.
- `querypilot eval run|replay|check|init` CLI with audit-log → regression-
  suite replay, baseline-vs-current regression detection, and a starter
  scaffold for `suites/` and `.eval/`.
- Sample GitHub Actions workflow at `.github/workflows/eval.yml` running
  `eval run` + `eval check` on every PR.

[Unreleased]: https://github.com/nickklos10/QueryPilot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nickklos10/QueryPilot/releases/tag/v0.1.0
