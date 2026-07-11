# QueryPilot Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the reproduced SQL access-policy and PostgreSQL read-only bypasses while preserving QueryPilot's public APIs, MCP tools, eval behavior, and PR #13 package metadata.

**Architecture:** Keep `SQLValidator` as the enforcement entrypoint, but derive statement, relation, column, star, and function decisions from the `sqlglot` AST. Add database-level defense in depth by making PostgreSQL queries start in a read-only transaction. All changes are test-first and introduce no mandatory dependency.

**Tech Stack:** Python 3.11+, Pydantic 2, SQLAlchemy 2, sqlglot 25+, psycopg 3, pytest 8+, MCP 1+

## Global Constraints

- Preserve the public `QueryPilot`, FastAPI, CLI, and MCP interfaces.
- Retain the MIT/package artifacts inherited from `origin/launch/pr-a-package-metadata`.
- Add no mandatory dependency.
- Treat database roles as the final privilege boundary; application validation must still fail closed.
- Write each regression test before its production change and observe the expected failure.

---

### Task 1: AST Statement and Function Safety

**Files:**
- Modify: `src/querypilot/core/config.py`
- Modify: `src/querypilot/validation/validator.py`
- Modify: `src/querypilot/validation/policies.py`
- Test: `tests/test_validation.py`

**Interfaces:**
- Consumes: `SafetyPolicy`, `sqlglot.exp.Expression`, `ValidationResult`.
- Produces: `SafetyPolicy.blocked_functions`, `SafetyPolicy.allowed_functions`, AST-based `statement_safety` and `function_safety` policy checks.

- [ ] **Step 1: Add failing regression tests for literals, nested writes, and default-blocked PostgreSQL functions**

Add tests that assert:

```python
def test_dangerous_word_inside_literal_or_comment_is_allowed(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")
    literal = qp.validate_sql("SELECT 'drop' AS harmless FROM customers")
    comment = qp.validate_sql("SELECT customer_name FROM customers -- drop is documentation")
    assert literal.valid is True
    assert comment.valid is True


def test_nested_write_expression_is_rejected(demo_db_url: str) -> None:
    schema = QueryPilot.connect(demo_db_url, dialect="sqlite").get_schema()
    validator = SQLValidator(QueryPilotConfig(dialect="postgres"))
    result = validator.validate(
        "WITH changed AS (DELETE FROM customers RETURNING id) SELECT * FROM changed",
        schema,
    )
    assert result.valid is False
    assert result.blocked_reason == "SQL contains a non-read-only operation: DELETE"


@pytest.mark.parametrize(
    "function_name",
    ["nextval", "setval", "pg_terminate_backend", "pg_cancel_backend",
     "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_sleep"],
)
def test_dangerous_postgres_functions_are_rejected(function_name: str) -> None:
    validator = SQLValidator(QueryPilotConfig(dialect="postgres"))
    result = validator.validate(f"SELECT {function_name}('x')", DatabaseSchema(dialect="postgres"))
    assert result.valid is False
    assert result.blocked_reason == f"SQL function is blocked by policy: {function_name}"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_validation.py -k 'dangerous_word or nested_write or dangerous_postgres' -v
```

Expected: literal/comment test fails under raw keyword scanning; nested write and function cases are accepted or fail for the wrong reason.

- [ ] **Step 3: Add configurable function policy and AST operation checks**

In `policies.py`, replace raw dangerous-keyword policy use with constants:

```python
BLOCKED_POSTGRES_FUNCTIONS = {
    "nextval", "setval", "pg_terminate_backend", "pg_cancel_backend",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_sleep",
}
```

In `SafetyPolicy`, add case-insensitive configurable fields:

```python
blocked_functions: list[str] = Field(
    default_factory=lambda: sorted(BLOCKED_POSTGRES_FUNCTIONS)
)
allowed_functions: list[str] | None = None
```

In `validator.py`, remove `_contains_dangerous_keyword`. Add helpers that:

```python
def _non_readonly_operation(expression: exp.Expression) -> str | None:
    blocked_types = (exp.Alter, exp.Command, exp.Copy, exp.Create, exp.Delete,
                     exp.Drop, exp.Insert, exp.Merge, exp.TruncateTable, exp.Update)
    for node in expression.walk():
        if isinstance(node, blocked_types):
            return type(node).__name__.upper()
    return None


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return function.name.lower()
    return function.sql_name().lower()
```

Walk `exp.Func` nodes, reject blocked names, and when `allowed_functions` is non-null reject names outside it. Emit one `function_safety` policy check; blocked functions take precedence over the allowlist.

- [ ] **Step 4: Add and verify optional allowlist tests**

```python
def test_function_allowlist_fails_closed() -> None:
    validator = SQLValidator(QueryPilotConfig(
        dialect="postgres",
        safety_policy=SafetyPolicy(allowed_functions=["count"]),
    ))
    schema = DatabaseSchema(dialect="postgres")
    assert validator.validate("SELECT COUNT(*)", schema).valid is True
    result = validator.validate("SELECT lower('A')", schema)
    assert result.valid is False
    assert result.blocked_reason == "SQL function is not allowed by policy: lower"
```

Run: `.venv/bin/pytest tests/test_validation.py -v`

Expected: all validation tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/querypilot/core/config.py src/querypilot/validation/policies.py src/querypilot/validation/validator.py tests/test_validation.py
git commit -m "fix: enforce AST statement and function safety"
```

---

### Task 2: Alias-Aware, Fail-Closed Column Policies

**Files:**
- Modify: `src/querypilot/validation/validator.py`
- Test: `tests/test_access_policy.py`
- Test: `tests/test_validation.py`

**Interfaces:**
- Consumes: parsed `exp.Select`, `DatabaseSchema`, `AccessPolicy`.
- Produces: `_RelationScope`, resolved `(table, column)` references, explicit star exposures, unknown/ambiguous column errors.

- [ ] **Step 1: Add failing tests for the three reproduced access-policy bypasses**

Add to `tests/test_access_policy.py`:

```python
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",
        "SELECT c.email FROM customers AS c",
        "SELECT email FROM customers JOIN orders ON customers.id = orders.customer_id",
    ],
)
def test_blocked_columns_cannot_be_bypassed(tenant_db_url: str, sql: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        access_policy=AccessPolicy(blocked_columns={"customers": ["email"]}),
    )
    result = qp.validate_sql(sql)
    assert result.valid is False
    assert result.blocked_reason == "Column is blocked by access policy: customers.email"
```

Extend the fixture with a schema-valid join target:

```python
conn.executescript(
    """
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL,
        amount INTEGER NOT NULL
    );
    INSERT INTO orders (customer_id, amount) VALUES (1, 1000);
    """
)
```

- [ ] **Step 2: Run bypass tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_access_policy.py -k 'cannot_be_bypassed' -v
```

Expected: star, alias, and multi-table unqualified cases are accepted.

- [ ] **Step 3: Implement relation scope and column resolution**

Add a focused internal model:

```python
@dataclass(frozen=True)
class _RelationScope:
    tables: tuple[str, ...]
    aliases: dict[str, str]

    def resolve_table(self, qualifier: str) -> str | None:
        normalized = _normalize_identifier(qualifier).lower()
        return self.aliases.get(normalized)
```

Build it from `exp.Table` nodes using `table.alias_or_name`. Resolve each non-star `exp.Column` as follows:

```python
if column.table:
    table_name = scope.resolve_table(column.table)
else:
    candidates = [name for name in scope.tables
                  if (table := schema.get_table(name)) and table.get_column(column.name)]
    if len(candidates) == 1:
        table_name = candidates[0]
    elif not candidates:
        errors.append(f"Unknown column: {column.name}")
    else:
        errors.append(f"Ambiguous column: {column.name}")
```

Use the same resolved references for known-column validation and `_access_policy_errors`.

- [ ] **Step 4: Implement star exposure enforcement**

Treat `exp.Column` with `column.name == "*"` and bare `exp.Star` as exposures. Resolve `alias.*` through the relation map; bare stars expose every table. For each exposed table:

```python
blocked_for_table = blocked.get(table_name, set())
if blocked_for_table:
    column = sorted(blocked_for_table)[0]
    errors.append(f"Column is blocked by access policy: {table_name}.{column}")

allowed_for_table = allowed.get(table_name)
if allowed_for_table is not None:
    exposed = {column.name.lower() for column in table_schema.columns}
    disallowed = sorted(exposed - allowed_for_table)
    if disallowed:
        errors.append(f"Column is not allowed by access policy: {table_name}.{disallowed[0]}")
```

- [ ] **Step 5: Add unknown/ambiguous and allowed-column tests**

Add these concrete cases:

```python
def test_allowed_columns_apply_through_alias_and_star(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(
        tenant_db_url,
        access_policy=AccessPolicy(allowed_columns={"customers": ["customer_name"]}),
    )
    alias = qp.validate_sql("SELECT c.revenue FROM customers AS c")
    star = qp.validate_sql("SELECT c.* FROM customers AS c")
    assert alias.blocked_reason == "Column is not allowed by access policy: customers.revenue"
    assert star.valid is False
    assert star.blocked_reason is not None
    assert star.blocked_reason.startswith("Column is not allowed by access policy: customers.")


def test_multitable_columns_fail_closed(tenant_db_url: str) -> None:
    qp = QueryPilot.connect(tenant_db_url)
    unknown = qp.validate_sql(
        "SELECT missing FROM customers JOIN orders ON customers.id = orders.customer_id"
    )
    ambiguous = qp.validate_sql(
        "SELECT id FROM customers JOIN orders ON customers.id = orders.customer_id"
    )
    qualified = qp.validate_sql(
        "SELECT customers.id, orders.amount FROM customers "
        "JOIN orders ON customers.id = orders.customer_id"
    )
    assert unknown.blocked_reason == "Unknown column: missing"
    assert ambiguous.blocked_reason == "Ambiguous column: id"
    assert qualified.valid is True
```

Run:

```bash
.venv/bin/pytest tests/test_access_policy.py tests/test_validation.py -v
```

Expected: all focused tests pass.

- [ ] **Step 6: Run the full suite and commit Task 2**

Run: `.venv/bin/pytest -q`

Expected: all tests pass; update only assertions whose former behavior represented a documented fail-open path.

```bash
git add src/querypilot/validation/validator.py tests/test_access_policy.py tests/test_validation.py
git commit -m "fix: resolve aliases and stars in column policies"
```

---

### Task 3: PostgreSQL Read-Only Transaction Enforcement

**Files:**
- Modify: `src/querypilot/connectors/postgres.py`
- Modify: `tests/test_postgres_connector.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `PostgresConnector.engine.connect()`, SQLAlchemy `Connection.execute`.
- Produces: ordered `SET TRANSACTION READ ONLY`, `SET LOCAL statement_timeout`, validated query execution.

- [ ] **Step 1: Add a failing connector-order test**

Add this fake-backed connector test:

```python
from types import SimpleNamespace


class _FakeResult:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []

    def close(self) -> None:
        return None

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, executed: list[str]) -> None:
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, statement):
        sql = str(statement)
        self.executed.append(sql)
        if sql == "SELECT 1 AS value":
            return _FakeResult([SimpleNamespace(_mapping={"value": 1})])
        return _FakeResult()


class _FakeEngine:
    def __init__(self, executed: list[str]) -> None:
        self.connection = _FakeConnection(executed)

    def connect(self):
        return self.connection


def test_execute_readonly_sets_transaction_before_timeout_and_query() -> None:
    executed: list[str] = []
    connector = object.__new__(PostgresConnector)
    connector.timeout_seconds = 10
    connector.engine = _FakeEngine(executed)

    rows, row_count = connector.execute_readonly("SELECT 1 AS value")

assert executed == [
    "SET TRANSACTION READ ONLY",
    "SET LOCAL statement_timeout = 10000",
    "SELECT 1 AS value",
]
assert rows == [{"value": 1}]
assert row_count == 1
```

- [ ] **Step 2: Run the connector test and verify RED**

Run: `.venv/bin/pytest tests/test_postgres_connector.py -v`

Expected: only timeout and query commands are recorded; read-only command is missing.

- [ ] **Step 3: Implement the minimal connector change**

At the start of `execute_readonly`:

```python
readonly_result = conn.execute(text("SET TRANSACTION READ ONLY"))
readonly_result.close()
timeout_result = conn.execute(
    text(f"SET LOCAL statement_timeout = {self.timeout_seconds * 1000}")
)
timeout_result.close()
```

Then execute and fetch the validated query as before.

- [ ] **Step 4: Document the database-role requirement**

Add this paragraph under the safety guidance:

```markdown
For PostgreSQL production use, connect QueryPilot with a dedicated
least-privilege role that has only the required schema `USAGE` and table
`SELECT` grants. QueryPilot requests a read-only transaction and applies a
statement timeout, but application validation is not a replacement for
database permissions.
```

- [ ] **Step 5: Verify and commit Task 3**

Run:

```bash
.venv/bin/pytest tests/test_postgres_connector.py tests/test_querypilot_flow.py -v
```

Expected: all focused tests pass.

```bash
git add src/querypilot/connectors/postgres.py tests/test_postgres_connector.py README.md
git commit -m "fix: enforce read-only PostgreSQL transactions"
```

---

### Task 4: End-to-End Security and Distribution Verification

**Files:**
- Modify: `suites/safety.yaml`
- Modify: `.eval/baseline.json` only if deterministic report structure requires it
- Test: `tests/test_mcp_runtime.py`

**Interfaces:**
- Consumes: complete validator, connector, MCP server, eval CLI, Hatch build.
- Produces: durable adversarial safety cases and verified distributable artifacts.

- [ ] **Step 1: Add safety-suite cases for confirmed bypasses**

Append these cases to `suites/safety.yaml`:

```yaml
  - id: blocks_privileged_function
    sql: "SELECT pg_read_file('/etc/passwd')"
    should_pass: false
    expected_failure_kind: validation
    expected_error_contains: ["SQL function is blocked by policy: pg_read_file"]
    tags: [safety, function]

  - id: blocks_nested_delete
    sql: "WITH changed AS (DELETE FROM customers RETURNING id) SELECT * FROM changed"
    should_pass: false
    expected_failure_kind: validation
    expected_error_contains: ["SQL contains a non-read-only operation: DELETE"]
    tags: [safety, mutation, cte]
```

Add this MCP regression test and import `AccessPolicy`:

```python
def test_mcp_access_policy_cannot_be_bypassed_by_alias_or_star(demo_db_url: str) -> None:
    qp = QueryPilot.connect(
        demo_db_url,
        dialect="sqlite",
        access_policy=AccessPolicy(blocked_columns={"customers": ["revenue"]}),
    )
    server = create_mcp_server(qp, fastmcp_cls=FakeFastMCP)

    alias_result = server.tools["execute_sql"](
        "SELECT c.revenue FROM customers AS c"
    )
    star_result = server.tools["execute_sql"]("SELECT * FROM customers")

    assert alias_result["error"].startswith("SQL validation failed")
    assert "customers.revenue" in alias_result["error"]
    assert star_result["error"].startswith("SQL validation failed")
    assert "customers.revenue" in star_result["error"]
    assert set(server.tools) == {
        "ask_database", "search_schema", "validate_sql", "execute_sql"
    }
```

- [ ] **Step 2: Run focused MCP and eval verification**

```bash
.venv/bin/pytest tests/test_mcp_runtime.py -v
.venv/bin/querypilot eval run --suite suites/smoke.yaml --generator demo --report /tmp/querypilot-smoke.json --no-color
.venv/bin/querypilot eval run --suite suites/safety.yaml --generator demo --report /tmp/querypilot-safety.json --no-color
```

Expected: MCP tests pass; smoke and safety reports have no threshold violations.

- [ ] **Step 3: Run complete verification**

```bash
.venv/bin/pip install build twine
.venv/bin/pytest -q
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

Expected: all tests pass, wheel and sdist build, and Twine reports both artifacts `PASSED`.

- [ ] **Step 4: Inspect package and Git state**

```bash
git diff --check
git status --short --branch
git log --oneline --decorate -6
```

Confirm `LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md`, and `src/querypilot/py.typed` remain present and no generated artifact is staged.

- [ ] **Step 5: Commit Task 4**

```bash
git add suites/safety.yaml tests/test_mcp_runtime.py .eval/baseline.json
git commit -m "test: cover SQL security regressions end to end"
```

If `.eval/baseline.json` did not change, omit it from `git add`. Do not commit `dist/`.
