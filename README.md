# QueryPilot

<!-- mcp-name: io.github.nickklos10/querypilot -->

[![PyPI](https://img.shields.io/pypi/v/querypilot.svg)](https://pypi.org/project/querypilot/)
[![Python](https://img.shields.io/pypi/pyversions/querypilot.svg)](https://pypi.org/project/querypilot/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/nickklos10/QueryPilot/actions/workflows/eval.yml/badge.svg)](https://github.com/nickklos10/QueryPilot/actions/workflows/eval.yml)

Safe, measurable SQL for AI agents.

QueryPilot sits between an LLM agent and your database. It gives the agent read-only, policy-checked SQL tools for any SQLAlchemy database. It grounds the model in your real schema, feeds errors back so the model can fix its SQL, and ships an eval harness that proves the SQL is correct and stays correct.

<p align="center"><img src="https://raw.githubusercontent.com/nickklos10/QueryPilot/main/docs/assets/eval-report.svg" alt="querypilot eval run terminal report" width="840"></p>

## Quickstart

```bash
pip install querypilot
querypilot demo
```

`querypilot demo` writes a small SaaS database (`querypilot-demo.db`), runs a validated query against it and shows a blocked `DELETE`. Then try:

```bash
querypilot schema --db sqlite:///querypilot-demo.db              # list tables
querypilot schema --db sqlite:///querypilot-demo.db invoices     # columns, keys, sample values
querypilot sql --db sqlite:///querypilot-demo.db "SELECT customer_nme FROM customers"
# Error: SQL validation failed: Unknown column: customer_nme
#   hint: Column 'customer_nme' does not exist; did you mean customers.customer_name, ...?
querypilot ask --db sqlite:///querypilot-demo.db "Which customers have the highest ARR?"
```

`ask` uses OpenAI or Anthropic when `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is set (`--generator openai-compatible` covers Ollama, vLLM and LM Studio). Without a key it falls back to an offline demo generator.

For your own database, `querypilot init --db postgresql://analyst@db.internal/prod` writes a `querypilot.yaml` that holds the connection, business hints, column descriptions and access policy. Every command, the MCP server and `QueryPilot.from_config()` read it, so you can drop `--db` from then on.

In Python:

```python
from querypilot import QueryPilot

qp = QueryPilot.connect("postgresql://analyst@db.internal/prod")  # dialect inferred from the URL

result = qp.execute_sql("SELECT plan, COUNT(*) FROM subscriptions GROUP BY plan")
print(result.sql)   # rewritten with a LIMIT, validated against the live schema
print(result.rows)
```

## Connect any database

`QueryPilot.connect(url)` infers the SQL dialect from the URL and introspects tables, views, primary and foreign keys, and comments. It also accepts a SQLAlchemy `Engine`.

| Database | URL | Install |
|---|---|---|
| SQLite | `sqlite:///app.db` | included |
| PostgreSQL | `postgresql://user:pass@host/db` | included |
| DuckDB | `duckdb:///analytics.duckdb` | `pip install "querypilot[duckdb]"` |
| MySQL / MariaDB | `mysql+pymysql://user:pass@host/db` | `pip install "querypilot[mysql]"` |
| SQL Server | `mssql+pyodbc://…` | `pip install "querypilot[mssql]"` |
| Snowflake | `snowflake://user@account/db/schema` | `pip install "querypilot[snowflake]"` |
| BigQuery | `bigquery://project/dataset` | `pip install "querypilot[bigquery]"` |
| Redshift | `redshift+psycopg2://…` | `pip install "querypilot[redshift]"` |
| Trino / Oracle / Databricks | `trino://…`, `oracle+oracledb://…`, `databricks://…` | `querypilot[trino]`, `[oracle]`, `[databricks]` |

Every query is validated with [sqlglot](https://github.com/tobymao/sqlglot) for the target dialect. Validation means one statement, SELECT only, and known tables and columns. It also blocks dangerous functions for each engine, such as `pg_read_file`, DuckDB's `read_csv`, MySQL's `LOAD_FILE` and SQL Server's `xp_cmdshell`. Execution then adds the engine's own guard: a read-only transaction on Postgres, MySQL and Oracle, a read-only connection on DuckDB, and a statement timeout where the engine supports one. Every query runs in a transaction that is always rolled back. For warehouses that have no read-only session mode, connect with a read-only role.

## Give your agent the tools

Every integration exposes the same small toolset. Results are compact JSON, and errors come back as `{"ok": false, "error": ..., "hints": [...]}` rather than as exceptions, so the agent can correct itself.

| Tool | What it does |
|---|---|
| `list_tables` | Tables and views the policy allows, with comments |
| `describe_table` | Columns, types, keys, comments and sample values for the named tables |
| `search_schema` | Finds tables and columns relevant to a question |
| `validate_sql` | Checks SQL and returns the rewritten form, errors and "did you mean" hints |
| `run_sql` | Validates and runs SQL, returning rows with a `truncated` flag at `max_rows` |
| `ask_database` | Natural language → validated SQL → rows (only when an LLM generator is configured) |

**MCP** (Claude Code, Claude Desktop, Cursor, VS Code, Windsurf):

```bash
querypilot mcp-config claude-code --db postgresql://analyst@db/prod
# claude mcp add querypilot --env QUERYPILOT_DATABASE_URL=... -- /path/to/uvx --from 'querypilot[mcp]' querypilot mcp
querypilot mcp-config cursor --db sqlite:///app.db   # prints the mcpServers JSON block
```

The generated config uses the absolute path to `uvx`, which avoids the common `spawn uvx ENOENT` failure. Pass `--runner python` to launch the current environment instead. Every tool carries the MCP `readOnlyHint` annotation. `querypilot mcp --transport streamable-http --port 8000` serves the same tools over HTTP.

**OpenAI / Anthropic tool calling:**

```python
tools = qp.as_openai_tools()            # Chat Completions; qp.toolkit().openai_responses_tools() for Responses
tools = qp.as_anthropic_tools()
result = qp.handle_tool_call(tool_call.name, tool_call.arguments)  # dict or JSON string in, dict out
```

**Agent frameworks:**

```python
from querypilot.integrations import (
    as_langchain_tools,          # LangChain / LangGraph StructuredTools
    as_llamaindex_tools,         # LlamaIndex FunctionTools
    as_openai_agents_tools,      # OpenAI Agents SDK FunctionTools
    as_claude_agent_mcp_server,  # Claude Agent SDK in-process MCP server
    claude_agent_allowed_tools,
    as_functions,                # plain typed functions: Pydantic AI, smolagents, CrewAI, ADK
)

tools = as_langchain_tools(qp, naming="langchain")  # sql_db_list_tables, sql_db_schema, sql_db_query, ...
```

`naming="langchain"` reuses the tool names from the community `SQLDatabaseToolkit` (no longer maintained), so existing prompts keep working. Install the matching extra: `querypilot[langchain]`, `[llamaindex]`, `[openai-agents]`, `[claude-agent]` or `[mcp]`. See [`examples/07_frameworks.py`](examples/07_frameworks.py).

## Protect any Postgres client: `querypilot proxy` (prototype)

The proxy needs no SDK. It is a Postgres wire-protocol server that applies the same validator and access policy to every statement, whatever the client. That covers psql, psycopg, SQLAlchemy and LangChain, an existing MCP database server, Cursor or Claude Code, and BI tools. Point the client's connection string at the proxy instead of the database:

```bash
pip install "querypilot[proxy]"
querypilot proxy --upstream postgresql://readonly@db/prod --listen 127.0.0.1:6543 --policy querypilot.yaml

psql "postgresql://agent@127.0.0.1:6543/prod" -c "SELECT email FROM customers"
npx -y @modelcontextprotocol/server-postgres postgresql://agent@127.0.0.1:6543/prod   # unchanged MCP server
```

Every statement then gets the following:

- **Read-only enforcement.** Writes, DDL, COPY and `SET` of anything outside a small allowlist are rejected. Allowed statements run in a `READ ONLY` transaction on a session that defaults to read-only, so `COMMIT; DROP TABLE …` is rejected.
- **Row filters and the row cap.** The rewritten SQL is what reaches the database, so the policy's `row_filters` hold under joins, subqueries and CTEs, and `LIMIT` is capped at `max_rows`.
- **Masking.** `masking_rules` apply by output position, so `upper(email) AS contact` is masked too.
- **Errors an agent can repair.** A rejection is a normal Postgres error with a SQLSTATE, the validator's message, and a `HINT`, for example `Column 'nope' does not exist. Columns of orders: id, customer_id, …`.
- **Audit.** One JSONL record per statement goes to `--audit-log`, with the client's SQL, the SQL that ran, the row count, and the client user and application.

Driver handshakes, `SET`/`SHOW` of formatting settings, and catalog queries for `psql \d` and ORM reflection pass through an allowlist. Catalog queries that could read data, such as `pg_stats` or `query_to_xml(...)`, are rejected. The policy file is a normal `querypilot.yaml`, and its `database_url` is used when you omit `--upstream`. Off loopback, set `QUERYPILOT_PROXY_PASSWORD`. Protocol coverage, known gaps (no TLS termination yet, no server-side cursors), and the reasons it fails closed are in [docs/proxy.md](docs/proxy.md).

## Better SQL from the model

When QueryPilot writes the SQL itself (`ask`, `ask_database`, evals), it uses the techniques with the largest measured gains in the text-to-SQL literature. The sources and numbers are in [docs/research/developer-needs.md](docs/research/developer-needs.md).

- **Grounded schema.** The prompt shows each table as compact DDL with primary keys, foreign-key join paths, comments and real values for category-like text columns: every value when there are 10 or fewer, otherwise the 3 most common. Sample values are never drawn from blocked, masked or row-filtered columns.
- **Your business rules.** Pass `hints=` and `descriptions=` to `connect()`, or set them in `querypilot.yaml`, for definitions such as "revenue means paid invoices only".
- **Pruning only when needed.** Below `schema_token_budget` (default 6000 tokens) the full schema is sent. Above it, QueryPilot sends the relevant tables plus the foreign-key bridge tables that join them.
- **A repair loop that converges.** Validation errors get "did you mean" hints. Database errors are cleaned before they go back to the model. An empty result triggers one gentle review. Up to 3 attempts by default.

```python
from querypilot import AnthropicSQLGenerator, QueryPilot

qp = QueryPilot.connect(
    "duckdb:///analytics.duckdb",
    generator=AnthropicSQLGenerator(),
    hints=["Revenue means SUM(amount_cents) / 100 over invoices with status = 'paid'"],
    descriptions={"accounts.segment": "smb, midmarket or enterprise"},
)
answer = qp.ask("Revenue by segment last quarter")
```

## Why QueryPilot exists

Read-only SQL access for agents is becoming a commodity. The hard part is making that access *measurably reliable*: proving the SQL the agent writes is correct, safe and fast, and that it doesn't regress.

QueryPilot can measure every change to your prompts, model or configuration against an execution-truth eval suite. You can write suites by hand, or generate them by replaying your audit log as a regression set. The queries that worked in production yesterday then have to keep working tomorrow.

## Eval quickstart

From a clone of this repository (the bundled suites run against `tests/fixtures/demo.db`):

```bash
pip install -e ".[eval]"
querypilot eval run \
    --suite suites/smoke.yaml \
    --generator demo \
    --report eval-out.json
querypilot eval check \
    --report eval-out.json \
    --baseline .eval/baseline.json \
    --threshold 0.9 \
    --require-safety 1.0
```

Sample output (abridged — see the full report at the top of this README):

```text
QueryPilot Eval Report
Suite:     smoke
Generator: demo

Overall
  ✅  Pass rate                       3 / 3 (100%)
  ✅  Safety pass rate                0 / 0 (100%)
  ✅  Correctness                     3 / 3 (100%)
  ✅  P95 latency                     18 ms

✅ No threshold violations.
```

The bundled `suites/smoke.yaml` runs against a tiny SQLite fixture (`tests/fixtures/demo.db`) so the harness works end-to-end without an LLM key. In your own project, `querypilot eval init` scaffolds `suites/` and `.eval/`. To benchmark a real generator, use `--generator openai` or `--generator anthropic`.

## Audit-Log Replay

`querypilot eval replay` turns a JSONL audit log written by `JSONLAuditSink` into a `BenchmarkSuite` whose gold SQL is the SQL that previously executed. Re-running that suite gates accuracy regressions against your own production traffic — the unique-to-QueryPilot capability the eval positioning rests on.

```bash
querypilot eval replay \
    --audit-jsonl audit.jsonl \
    --fixture-db sqlite:///tests/fixtures/demo.db \
    --output suites/replay.yaml
querypilot eval run --suite suites/replay.yaml --generator demo --report replay-out.json
```

Conservative defaults: only successful `ask` records, non-empty results, no active access policy. `--include-failures`, `--include-masked`, `--include-empty` relax each filter.

## CI Gate

`querypilot eval check` compares a `SuiteReport` JSON against thresholds and a committed baseline, exiting non-zero on regression. A sample GitHub Actions workflow ships at `.github/workflows/eval.yml`:

```yaml
- run: querypilot eval run --suite suites/smoke.yaml --generator demo --report eval-out.json
- run: querypilot eval check --report eval-out.json --baseline .eval/baseline.json --threshold 0.9 --require-safety 1.0
```

When a regression is detected the output explains which cases regressed and how:

```text
Regression detected.

Pass rate:
  baseline: 96%
  current:  89%

Failed cases (regression vs. baseline):
  - monthly_revenue_by_segment (was passing -> now result_mismatch)
  - top_customers_by_arr      (was passing -> now repair_failed)

Latency:
  baseline p95: 2100 ms
  current p95:  3800 ms  (+1700 ms)
```

Refresh the baseline on `main` after a deliberate change:

```bash
querypilot eval run --suite suites/smoke.yaml --generator demo --report .eval/baseline.json
git commit -am "Refresh eval baseline"
```

## Authoring a Suite

Suites are YAML or JSON. Each case carries a question, a gold SQL, and the schema/safety expectations for the candidate.

```yaml
name: saas_revenue_suite
fixture_db: sqlite:///fixtures/demo.db
fixture_dialect: sqlite

thresholds:
  pass_rate: 0.95
  safety_pass_rate: 1.0
  correctness_rate: 0.9
  max_p95_latency_ms: 5000
  max_avg_cost_usd: 0.01

comparison:
  ignore_row_order: true
  ignore_column_order: true
  float_tolerance: 0.001
  normalize_datetimes: true

cases:
  - id: top_customers_by_revenue
    question: "Top customers by revenue"
    gold_sql: |
      SELECT customer_name, revenue
      FROM customers
      ORDER BY revenue DESC
      LIMIT 100
    expected_tables: [customers]
    must_include: ["ORDER BY", "LIMIT"]
    must_not_contain: [DELETE, UPDATE, DROP]
    tags: [revenue, ranking]

  - id: blocks_drop_table
    sql: "DROP TABLE customers"
    should_pass: false
    expected_failure_kind: validation
    expected_error_contains: ["Only SELECT queries are allowed"]
    tags: [safety, ddl]
```

Result-set correctness is scored by **executing both the gold and candidate SQL** against the same fixture database and comparing rows. Order-insensitive by default; auto-flipped to order-sensitive when the gold SQL has a top-level `ORDER BY`.

## Examples

Runnable, self-contained examples live in [`examples/`](examples/). They all use
the bundled demo SQLite fixture, so most need no API key:

| Example | Shows | Key? |
| --- | --- | --- |
| [`01_quickstart.py`](examples/01_quickstart.py) | connect, `execute_sql`, offline `ask()`, validation risk level | No |
| [`02_openai_tool_use.py`](examples/02_openai_tool_use.py) | `as_openai_tools()` in an OpenAI tool-use loop | `OPENAI_API_KEY` |
| [`03_anthropic_tool_use.py`](examples/03_anthropic_tool_use.py) | `as_anthropic_tools()` in an Anthropic tool-use loop | `ANTHROPIC_API_KEY` |
| [`04_access_control.py`](examples/04_access_control.py) | blocked columns, row filter, and masking | No |
| [`05_custom_eval_suite/`](examples/05_custom_eval_suite/) | a custom YAML suite run with `querypilot eval run`/`check` | No |
| [`06_mcp/`](examples/06_mcp/) | run `querypilot mcp` + a paste-ready Claude MCP config | No |
| [`07_frameworks.py`](examples/07_frameworks.py) | the same tools in LangChain, OpenAI Agents SDK and Claude Agent SDK | No |

See [`examples/README.md`](examples/README.md) for setup and the full index.

## LLM SQL Generation

For production-style natural-language SQL generation, plug in an LLM generator. QueryPilot still treats model output as an untrusted candidate: it validates, rewrites, and can ask the generator for a repair before execution.

Install optional provider dependencies:

```bash
pip install "querypilot[openai]"      # or querypilot[anthropic], or querypilot[llm] for both
```

OpenAI:

```python
from querypilot import QueryPilot
from querypilot.generation import OpenAISQLGenerator

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    generator=OpenAISQLGenerator(model="gpt-5.1"),
)
```

Anthropic:

```python
from querypilot import QueryPilot
from querypilot.generation import AnthropicSQLGenerator

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    generator=AnthropicSQLGenerator(model="claude-sonnet-4-20250514"),
)
```

### Local / open models

Any OpenAI-compatible endpoint — [Ollama](https://ollama.com), vLLM, LM Studio,
or llama.cpp's server — works through `OpenAICompatibleSQLGenerator`. It reuses
the `[openai]` extra (no extra dependency) and talks the Chat Completions API, so
you can benchmark open models at **$0**. The API key is optional (local servers
ignore it), and cost reports show `$0` while token counts still flow through when
the server returns usage.

```bash
ollama pull llama3.1
pip install "querypilot[openai]"
```

```python
from querypilot import QueryPilot
from querypilot.generation import OpenAICompatibleSQLGenerator

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    generator=OpenAICompatibleSQLGenerator(
        model="llama3.1",
        base_url="http://localhost:11434/v1",  # Ollama's default; omit to use it
    ),
)
```

From the eval harness, add open models to the benchmark matrix with
`--generator openai-compatible`:

```bash
querypilot eval run \
    --suite suites/smoke.yaml \
    --generator openai-compatible \
    --model llama3.1 \
    --base-url http://localhost:11434/v1 \
    --report eval-out.json
```

`--base-url` also reads `$QUERYPILOT_BASE_URL`, and defaults to Ollama's
`http://localhost:11434/v1` when unset.

The safety loop is always:

```text
question
  -> schema-scoped prompt
  -> model candidate SQL
  -> QueryPilot validation
  -> optional repair
  -> safe execution
```

## Eval Harness (Library)

The CLI is a thin wrapper around `run_suite`, which is also usable directly:

```python
from querypilot import QueryPilot
from querypilot.evals import (
    BenchmarkCase,
    BenchmarkSuite,
    NullCostTracker,
    build_qp_factory,
    render_terminal,
    run_suite,
)
from querypilot.generation.sql_generator import DemoSQLGenerator

suite = BenchmarkSuite(
    name="adhoc",
    fixture_db="sqlite:///tests/fixtures/demo.db",
    cases=[
        BenchmarkCase(
            id="count_customers",
            question="Count of customers",
            gold_sql="SELECT COUNT(*) AS count FROM customers",
            expected_tables=["customers"],
        ),
    ],
)

qp_factory = build_qp_factory(
    database_url="sqlite:///tests/fixtures/demo.db",
    generator=DemoSQLGenerator(),
)

report = run_suite(
    suite,
    qp_factory=qp_factory,
    cost_tracker_factory=NullCostTracker,
)

print(render_terminal(report, color=False))
```

The returned `SuiteReport` is a Pydantic model with `pass_rate`, `safety_pass_rate`, `correctness_rate`, `repair_rate`, `p50_latency_ms`, `p95_latency_ms`, `total_prompt_tokens`, `estimated_cost_usd`, `tag_rollups`, `failure_breakdown`, `threshold_violations`, and the full per-case `case_results` list.

## Safety Engine

QueryPilot validates SQL before execution with:

- `sqlglot` parsing
- single-statement enforcement
- SELECT-only read-only policy
- blocked keyword detection
- known table checks
- column checks where feasible
- allowed/blocked table policy
- automatic `LIMIT` insertion and max-row capping
- `SELECT *` warnings or rejection
- Cartesian join detection
- structured policy checks
- query fingerprints
- risk levels: `low`, `medium`, `high`, `critical`

For PostgreSQL production use, connect QueryPilot with a dedicated
least-privilege role that has only the required schema `USAGE` and table
`SELECT` grants. QueryPilot requests a read-only transaction and applies a
statement timeout, but application validation is not a replacement for
database permissions.

Example:

```python
validation = qp.validate_sql("SELECT * FROM customers")

print(validation.valid)
print(validation.risk_level)
print(validation.query_fingerprint)
print(validation.policy_checks)
```

For stricter deployments:

```python
from querypilot.core.config import SafetyPolicy

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    safety_policy=SafetyPolicy(
        allow_select_star=False,
        reject_cartesian_joins=True,
    ),
)
```

## FastAPI Server

Run QueryPilot as a local safe SQL gateway:

```bash
pip install "querypilot[server]"
querypilot serve --db sqlite:///demo.db --max-rows 100
```

Or use environment variables:

```bash
export QUERYPILOT_DATABASE_URL=sqlite:///demo.db
querypilot serve
```

Endpoints:

- `GET /health`
- `GET /schema`
- `POST /search-schema`
- `POST /ask`
- `POST /generate-sql`
- `POST /validate-sql`
- `POST /execute-sql`
- `POST /evals/run`
- `GET /audit/recent`

Example:

```bash
curl -X POST http://127.0.0.1:8000/validate-sql \
  -H "content-type: application/json" \
  -d '{"sql": "SELECT * FROM customers"}'
```

## Audit Trail

QueryPilot records structured audit events for schema search, SQL generation, validation, execution, and full `ask()` flows.

Each audit record can include:

- `audit_id`
- timestamp
- operation
- question
- original SQL
- rewritten SQL
- validation metadata
- execution status
- row count
- execution time
- error
- actor/session/application/trace metadata

Use the default in-memory sink:

```python
from querypilot import QueryPilot
from querypilot.audit import AuditMetadata

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    audit_metadata=AuditMetadata(
        actor="agent-1",
        session_id="session-1",
        app_name="analytics-agent",
    ),
)

result = qp.execute_sql("SELECT customer_name FROM customers")

print(result.audit_id)
print(qp.get_audit_records(limit=10))
```

Or persist JSONL audit events:

```python
from querypilot import QueryPilot
from querypilot.audit import JSONLAuditSink

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    audit_sink=JSONLAuditSink("querypilot-audit.jsonl"),
)
```

## Access Control

Read-only SQL is necessary but not enough. QueryPilot can also enforce column-level and row-level access policies before execution.

```python
from querypilot import QueryPilot
from querypilot.access import AccessPolicy, MaskingRule

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    access_policy=AccessPolicy(
        blocked_columns={
            "customers": ["email"],
        },
        row_filters={
            "customers": "tenant_id = 42",
        },
        masking_rules={
            "customers": {
                "email": MaskingRule(mode="redact"),
            },
        },
    ),
)
```

What this does:

- rejects SQL that selects blocked columns
- rejects SQL outside an allowlist when `allowed_columns` is configured
- injects required row filters such as `tenant_id = 42`
- masks configured result columns after execution
- records the applied access policy in validation, result, answer, and audit metadata

The server and MCP runtimes can also receive access policy JSON:

```bash
querypilot serve \
  --database-url sqlite:///demo.db \
  --access-policy-json '{
    "row_filters": {"customers": "tenant_id = 42"},
    "blocked_columns": {"customers": ["ssn"]}
  }'
```

## Current Scope

Shipped:

- `pip install querypilot`, `querypilot demo/schema/sql/ask/init/mcp-config`, and a `querypilot.yaml` project config
- connectors for SQLite and PostgreSQL built in, plus DuckDB, MySQL/MariaDB, SQL Server, Snowflake, BigQuery, Redshift, Trino, Oracle and Databricks through SQLAlchemy extras
- dialect-aware SQL validation and rewriting (sqlglot), per-engine dangerous-function blocklists, and layered read-only execution
- one agent toolkit exposed for MCP, OpenAI, Anthropic, LangChain, LlamaIndex, OpenAI Agents SDK, Claude Agent SDK and plain functions
- grounded generation: keys, foreign keys, comments, policy-safe sample values, business hints, and threshold-gated schema pruning
- OpenAI, Anthropic and OpenAI-compatible (Ollama, vLLM, LM Studio) generators with a repair loop for validation errors, database errors and empty results
- column policies, row filters and result masking
- in-memory and JSONL audit logging
- FastAPI server and MCP server runtimes
- **eval-driven harness**: YAML/JSON suites, execution-truth correctness scoring, safety/repair/latency/cost metrics, per-tag rollups, failure-category breakdown, threshold violations, JSON and terminal reports
- **audit-log → regression suite** (`querypilot eval replay`), **CI regression gate** (`querypilot eval check`), and `querypilot eval init`
- the SaaSPulse contamination-proof benchmark ([`benchmarks/`](benchmarks/))

## Roadmap

The ranked roadmap and the evidence behind it are in [docs/roadmap.md](docs/roadmap.md) and [docs/research/developer-needs.md](docs/research/developer-needs.md). Next up: multi-candidate generation with result voting, few-shot retrieval from verified queries, warehouse cost guards (dry runs, bytes-billed caps), and multi-database MCP servers.
