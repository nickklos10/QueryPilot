# Changelog

All notable changes to QueryPilot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Zero-config quickstart.** `querypilot demo` creates a demo database and runs
  a validated query. `querypilot schema`, `querypilot sql` and `querypilot ask`
  work against any `--db` URL. `querypilot init` writes a `querypilot.yaml`
  (connection, hints, descriptions, access policy, generator) that the CLI, the
  servers and the new `QueryPilot.from_config()` all read.
  `querypilot mcp-config <claude-code|claude-desktop|cursor|vscode|windsurf>`
  prints a ready-to-paste MCP config that uses an absolute `uvx` path; with
  `--config` (or a `./querypilot.yaml`) it sets `QUERYPILOT_CONFIG` to the
  config's absolute path, so the server keeps its policy and hints when the
  client starts it from another directory.
  `python -m querypilot` works too.
- **Connect any SQLAlchemy database.** `QueryPilot.connect(url)` infers the
  sqlglot dialect from the URL or from an `Engine`. A new `SQLAlchemyConnector`
  covers DuckDB, MySQL/MariaDB, SQL Server, Snowflake, BigQuery, Redshift, Trino,
  Oracle, Databricks and ClickHouse; each has a pip extra, and a missing driver
  raises an error that names the extra. Introspection now captures primary keys,
  foreign keys, comments, views and non-default schemas (`schemas=`), and the
  schema is cached (`refresh_schema()` re-reads it). Every query runs in a
  rolled-back transaction, plus the engine's own read-only mode or timeout
  where it has one. The default blocked functions now include DuckDB file
  readers, MySQL `LOAD_FILE`/`SLEEP`, SQL Server `xp_cmdshell`/`OPENROWSET`,
  SQLite `load_extension` and Postgres `lo_import`/`dblink`.
- **One agent toolkit everywhere.** `QueryPilotToolkit` (`qp.toolkit()`)
  provides `list_tables`, `describe_table`, `search_schema`, `validate_sql`,
  `run_sql` and `ask_database`. Results are compact JSON with a `truncated`
  flag, and errors come back as `{"ok": false, "error", "hints"}` payloads
  instead of exceptions. The toolkit is exposed as OpenAI Chat and Responses
  tools, Anthropic tools, `qp.handle_tool_call()`, an MCP server with
  `readOnlyHint` annotations, LangChain tools (`naming="langchain"` gives
  drop-in `sql_db_*` names), LlamaIndex, OpenAI Agents SDK, Claude Agent SDK
  (in-process MCP server) and plain functions (`querypilot.integrations`).
- **Grounded generation.** Prompts render compact DDL with primary keys,
  foreign-key join paths, comments, up to 3 sample values (every value for enums of 10 or fewer) per category-like
  text column, and business `hints=`/`descriptions=`. Sample values never come
  from blocked, masked or row-filtered columns, from columns whose names suggest
  secrets, personal data or free text (names, companies, addresses, IPs, URLs,
  salaries, notes, descriptions, ...), or from columns with more than 50
  distinct values in the scan or no repeated value. Above `schema_token_budget`, only relevant
  tables are sent, plus the FK bridge tables between them.
- **Exact-columns prompt rule.** The prompt asks for exactly the columns the
  question names, in that order. On a 220-question BIRD dev sample (Claude Haiku),
  QueryPilot's accuracy goes from 42.5% to 57.0% with this PR; see
  `benchmarks/results/2026-10-20-agent-dx.md`.
- **Repair that converges.** Validation errors carry "did you mean"
  suggestions; database errors are cleaned and fed back; an empty result gets
  one gentle review. `ask()` and the eval pipeline share one loop, and the
  default is now 3 attempts.
- Research and roadmap: `docs/research/developer-needs.md` (about 250 sourced
  developer pain points) and `docs/roadmap.md`.
- Behaviour changes:
  - `QueryPilot.connect` no longer defaults to `dialect="sqlite"`; it infers
    the dialect, so `postgresql://` URLs no longer build a SQLite connector.
  - `handle_anthropic_tool_call` returns error payloads instead of raising.
  - Advertised tool lists replace `execute_sql` with `run_sql`; `execute_sql`
    still dispatches as an alias.
  - Execution failures in `ask()` raise `ValueError("SQL execution failed: ...")`.
  - The MCP server runs on mcp 2.x (`MCPServer`) as well as 1.x. With mcp 2.x,
    importing `mcp.server.fastmcp` failed.
  - The MCP registry entry installs `querypilot[mcp]`. PyYAML is now a core
    dependency, for `querypilot.yaml`.
  - Tool results (`run_sql`, `ask_database`) return rows as arrays with a
    separate `columns` list, not as dicts. `search_schema` matches list their
    matching columns as `columns` (a list) instead of `column`.
  - Agent- and model-facing errors no longer leak what the access policy
    hides: database errors from `run_sql` and the `ask()` repair loop are
    cleaned, get "did you mean" hints, and have quoted values redacted (e.g.
    `could not convert string '<redacted>'`); policy errors that name a hidden
    table or column become one generic "restricted by the access policy"
    message (also in the `ValueError` from `execute_sql()`/`ask()`). Audit
    records and `ValidationResult.errors` keep the original errors.
  - When row-level filters apply, `run_sql`, `validate_sql` and `ask_database`
    return the agent's own SQL plus a note instead of the rewritten SQL; the
    rewritten SQL stays in the audit record. `QueryPilotAnswer.generated_sql`
    holds the SQL the generator wrote.
  - `QueryPilot.search_schema()` and the server's `/schema` and
    `/search-schema` endpoints use the policy-visible schema
    (`QueryPilot.visible_schema()`).
  - The schema cache is shared with `with_audit_metadata()` copies, so the
    server no longer re-introspects per request; `refresh_schema()` reaches
    every copy and also clears cached sample values.
  - `SQLAlchemyConnector`: MariaDB reached through a `mysql://` URL gets
    `max_statement_time` instead of `MAX_EXECUTION_TIME`; for a SQLite
    `Engine`, `PRAGMA query_only` is restored to its previous value even if the
    rollback fails, and queries are stopped at `timeout_seconds`.

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
- **SaaSPulse native benchmark** (`benchmarks/`) — a contamination-proof,
  execution-truth text-to-SQL suite on a novel, never-published B2B-SaaS schema
  (8 tables: plans, accounts, users, subscriptions, invoices, payments,
  support_tickets, usage_events; realistic FKs, NULLs, and edge rows). 102 cases:
  65 correctness cases across tagged `easy`/`medium`/`hard` tiers (each question
  fully specifies output columns, filters, ordering, and limits, with boring
  canonical gold SQL) and 37 safety cases (`should_pass: false`) covering DDL/DML,
  multi-statement, comment-smuggled injection, CTE-hidden mutations, PRAGMA/ATTACH,
  blocked functions, Cartesian joins, and schema exfiltration. Includes a
  deterministic, byte-stable fixture generator
  (`benchmarks/fixtures/make_saaspulse.py`, fixed seed, no `datetime.now()`), the
  committed `saaspulse.db` (~3.6 MB), a `validate_golds.py` pre-flight that
  executes every gold and asserts every unsafe case is blocked, and a
  `benchmarks/README.md` documenting regeneration, the `eval run` / `eval
  leaderboard` matrix, and the contamination-proof rationale.
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

### Changed

- Minimum `sqlglot` is now 25.20; earlier releases lack `exp.Alter` and
  already failed the test suite.

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
- The validator resolves columns through sqlglot's scope analysis, so common
  read-only queries are no longer rejected: `ORDER BY` / `HAVING` / `GROUP BY` on select
  aliases, CTEs (including recursive CTEs and CTE column lists), `UNION` /
  `INTERSECT` / `EXCEPT`, derived-table qualifiers, unqualified columns inside
  `IN (...)` subqueries, and joins nested in derived tables. CTE names are no
  longer reported in `ValidationResult.tables`.
- Opening a missing SQLite file raises `FileNotFoundError` instead of silently
  creating an empty database, and `querypilot eval run` exits with
  `Fixture database not found: <path>` before running any case.

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
