# QueryPilot

QueryPilot is a safe SQL tool layer for AI agents. It gives agents controlled access to relational databases through schema discovery, SQL generation, validation, read-only execution, and result explanation.

This first slice is offline-first: SQLite works end to end, PostgreSQL has a connector structure through SQLAlchemy, and natural-language generation uses a deterministic demo generator until an LLM provider is configured.

## Why QueryPilot Exists

Agent SDKs make it easy for models to call tools. They do not, by themselves, make database access safe.

QueryPilot sits between agents and relational databases. Every query flows through schema awareness, SQL parsing, policy checks, read-only enforcement, limit rewriting, and structured validation metadata before execution.

## Install For Local Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Quick Start

```python
from querypilot import QueryPilot

qp = QueryPilot.connect(
    database_url="sqlite:///demo.db",
    dialect="sqlite",
    readonly=True,
    max_rows=100,
)

result = qp.execute_sql("SELECT * FROM customers")

print(result.sql)
print(result.rows)
```

Natural-language `ask()` works offline for simple demo questions through a deterministic generator:

```python
answer = qp.ask("Which customers generated the most revenue?")

print(answer.sql)
print(answer.rows)
print(answer.validation.risk_level)
```

## LLM SQL Generation

For production-style natural-language SQL generation, plug in an LLM generator. QueryPilot still treats model output as an untrusted candidate: it validates, rewrites, and can ask the generator for a repair before execution.

Install optional provider dependencies:

```bash
.venv/bin/pip install -e ".[openai]"
.venv/bin/pip install -e ".[anthropic]"
```

OpenAI:

```python
from querypilot import QueryPilot
from querypilot.generation import OpenAISQLGenerator

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    generator=OpenAISQLGenerator(model="gpt-5.1"),
    max_generation_attempts=2,
)

answer = qp.ask("Which customers generated the most revenue?")
```

Anthropic:

```python
from querypilot import QueryPilot
from querypilot.generation import AnthropicSQLGenerator

qp = QueryPilot.connect(
    "sqlite:///demo.db",
    generator=AnthropicSQLGenerator(model="claude-sonnet-4-20250514"),
    max_generation_attempts=2,
)
```

The safety loop is always:

```text
question
  -> schema-scoped prompt
  -> model candidate SQL
  -> QueryPilot validation
  -> optional repair
  -> safe execution
```

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

## Agent Tool Adapters

QueryPilot exposes tool schemas without requiring SDK dependencies:

```python
openai_tools = qp.as_openai_tools()
anthropic_tools = qp.as_anthropic_tools()
```

Available tools:

- `ask_database`
- `search_schema`
- `validate_sql`
- `execute_sql`

## Evaluation Harness

QueryPilot includes a small eval runner so safety behavior can be tested as a product feature:

```python
from querypilot.evals.cases import EvalCase
from querypilot.evals.runner import run_eval_cases

report = run_eval_cases(
    qp,
    [
        EvalCase(
            name="safe customer revenue query",
            sql="SELECT customer_name, revenue FROM customers",
            expected_tables=["customers"],
            expected_sql_contains=["LIMIT 100"],
            must_not_contain=["DROP", "UPDATE"],
            should_pass=True,
        ),
        EvalCase(
            name="drop table is blocked",
            sql="DROP TABLE customers",
            should_pass=False,
        ),
    ],
)

print(report.passed, report.failed)
```

## Current Scope

Included now:

- installable Python package
- SQLite connector
- PostgreSQL connector structure
- schema introspection
- SQL validation and rewriting
- safe read-only execution
- offline demo SQL generation
- OpenAI and Anthropic tool schema adapters
- safety eval harness

Deferred:

- hosted LLM provider integration
- FastAPI server mode
- MCP server
- LangChain adapter
- Snowflake, BigQuery, Databricks, and Redshift connectors
- authentication and audit storage
