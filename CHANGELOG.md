# Changelog

All notable changes to QueryPilot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `ComparisonConfig.ignore_column_names` (suite-YAML-settable) — compare
  candidate results to gold by **values only**, ignoring the column aliases the
  model chose (Spider/BIRD "execution accuracy"). Column count must still match;
  a candidate passes when some permutation of its columns reproduces gold's
  value rows under the existing normalizations (row-order-insensitivity, float
  tolerance, datetime/case/NaN handling). The permutation is found with
  backtracking pruned by column value-multiset compatibility — not brute-force
  factorial — and wide result sets (>16 columns) error clearly rather than run
  unbounded. Fixes false-negatives where identical values failed on an alias
  mismatch (e.g. `COUNT(*) AS customer_count` vs gold `COUNT(*) AS count`); the
  accepted trade is that two columns with identical value multisets may swap and
  still match. The Spider/BIRD importer (`querypilot eval import`) now emits
  suites with `ignore_column_names: true` by default (matching upstream
  scoring), with `--no-ignore-column-names` to write name-aware suites instead.
- `querypilot eval leaderboard` — aggregate N `SuiteReport` JSONs (the same
  suite run against different generators/models) into a ranked comparison of
  pass rate, safety, correctness, repair rate, p50/p95 latency, cost, and
  tokens. Ranks by pass rate (tie-broken by safety then cost), warns when
  reports disagree on suite name or case count, and refuses to mix different
  suites without `--force`. Renders an aligned terminal table (matching
  `eval run`), a GitHub-flavored markdown table for blog posts, and
  machine-readable JSON (`--output` / `--format`). New
  `querypilot.evals.leaderboard` module.
- Dataset importer (`querypilot eval import`) that converts locally-downloaded
  **Spider 1.0** and **BIRD** dev sets into QueryPilot benchmark suites. It is
  file-based only (no network, no vendored dataset data — both are CC BY-SA),
  auto-detects Spider vs BIRD (or takes `--format`), writes **one suite YAML per
  `db_id`** (a `BenchmarkSuite` binds a single `fixture_db`, so a many-db dataset
  maps to a directory of per-db suites), carries BIRD `evidence` into the
  question, supports `--limit`/`--db` filtering with stable case ids, validates
  that each referenced SQLite fixture exists, and warns+skips cases whose gold
  SQL fails to execute (`--strict` to error instead). New
  `querypilot.evals.datasets` module with `import_dataset` / `detect_format` /
  `ImportResult` exports.
- `OpenAICompatibleSQLGenerator` for OpenAI-compatible local endpoints (Ollama,
  vLLM, LM Studio, llama.cpp). Reuses the `[openai]` extra (no new dependency),
  talks the Chat Completions API, defaults to Ollama's
  `http://localhost:11434/v1`, and treats the API key as optional. Wired into
  the eval harness as `--generator openai-compatible` with a `--base-url` flag
  (also `$QUERYPILOT_BASE_URL`), so the benchmark matrix can include open models
  at $0.
- `LocalCostTracker` — captures token usage from local endpoints when reported
  but always estimates $0, so reports show real token counts without bogus
  dollar figures. Paired automatically with the `openai-compatible` generator.

### Fixed

- `MODEL_PRICING` in `querypilot.evals.cost` predated the 2026 model lineup, so
  hosted models such as `gpt-5.4-nano` silently estimated **$0** cost —
  corrupting the benchmark's cost-per-query headline. Refreshed the table
  against the official OpenAI
  (<https://developers.openai.com/api/docs/pricing>) and Anthropic
  (<https://platform.claude.com/docs/en/about-claude/pricing>) pricing pages as
  of 2026-07-11 (GPT-5.4/5.5/5.6 tiers; Claude Opus 4.8/4.7, Sonnet 5,
  Haiku 4.5), with a source-URL + as-of-date comment block. Model ids now
  resolve by exact match then longest-prefix family fallback, so dated
  snapshots (e.g. `gpt-5.4-nano-2026-03-17`) inherit their family price.
  Models still missing from the table now emit a one-time `warnings.warn`
  naming the model and fall back to $0 (previously silent), while
  `LocalCostTracker` and `NullCostTracker` remain warning-free $0 by design.

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
