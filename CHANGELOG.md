# Changelog

All notable changes to QueryPilot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Row filters are now applied to every reference of a filtered table, at every
  scope, by wrapping it as `(SELECT * FROM t WHERE <filter>) AS t`. Previously
  the filter was appended unqualified to the outermost `WHERE`, so scalar
  subqueries, `IN (...)` subqueries, and derived tables read other tenants' rows,
  a derived column such as `42 AS tenant_id` satisfied the filter, and the filter
  could bind to a same-named column of another table. Outer joins keep their
  semantics. Rewritten SQL for filtered queries changes shape accordingly.
- Masking follows column lineage by output position (new
  `ValidationResult.output_masks`) instead of matching result keys, so aliases
  (`email AS contact`), expressions (`LOWER(email)`, `'x' || email`), CTEs,
  derived tables, scalar subqueries, and set operations no longer return raw
  masked values. `COUNT(masked_column)` is left unmasked. If the returned row
  shape does not match the analyzed projection, every value is masked.
- `max_rows` can no longer be bypassed with `LIMIT -1`, `LIMIT` expressions or
  subqueries, or `FETCH`; anything but a non-negative integer literal within
  policy is replaced.
- `SELECT ... INTO` is rejected as a non-read-only operation.
- SQLite databases are opened read-only (`mode=ro`), matching the Postgres
  `READ ONLY` transaction, so a validator gap cannot become a write.

### Fixed

- The validator resolves columns through sqlglot's scope analysis, so common
  read-only queries are no longer rejected: `ORDER BY` / `HAVING` on select
  aliases, CTEs (including recursive CTEs and CTE column lists), `UNION` /
  `INTERSECT` / `EXCEPT`, derived-table qualifiers, unqualified columns inside
  `IN (...)` subqueries, and joins nested in derived tables. CTE names are no
  longer reported in `ValidationResult.tables`.
- Opening a missing SQLite file raises `FileNotFoundError` instead of silently
  creating an empty database, and `querypilot eval run` exits with
  `Fixture database not found: <path>` before running any case.

### Changed

- Minimum `sqlglot` is now 25.20; earlier releases lack `exp.Alter` and
  already failed the test suite.

## [0.1.1] - 2026-07-11

### Added

- `server.json` MCP-registry manifest at the repo root and an `mcp-name`
  ownership marker in the README, preparing the official MCP registry
  submission.
- README hero image (`docs/assets/eval-report.svg`) — a self-contained SVG
  rendering of the `querypilot eval run` terminal report.
- First-class [`examples/`](examples/) directory: offline quickstart, OpenAI
  and Anthropic tool-use loops, an access-control walkthrough (blocked columns,
  row filters, masking), a custom eval suite with `querypilot eval run`/`check`,
  and an MCP server guide with a paste-ready Claude Desktop / Claude Code config.
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

[Unreleased]: https://github.com/nickklos10/QueryPilot/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/nickklos10/QueryPilot/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nickklos10/QueryPilot/releases/tag/v0.1.0
