# QueryPilot Security Hardening Design

## Objective

Harden QueryPilot's SQL validator and PostgreSQL connector so the documented
read-only and column-access guarantees fail closed for the bypasses reproduced
during review. Preserve MCP, server, library, and eval compatibility, and build
on the MIT/package metadata already present in PR #13.

## Scope

This change will:

- enforce blocked and allowed column policies through table aliases;
- reject `SELECT *` when it could expose a blocked or non-allowlisted column;
- resolve unqualified columns in multi-table queries against the schema and
  fail closed when resolution is unknown or ambiguous;
- stop treating dangerous words inside string literals or comments as SQL
  operations;
- reject dangerous PostgreSQL functions that can mutate state, terminate
  sessions, inspect server files, or create denial-of-service behavior;
- expose configurable function deny/allow policy through `SafetyPolicy`;
- execute PostgreSQL queries in a database-enforced read-only transaction;
- add regression coverage for each confirmed bypass and connector behavior;
- retain PR #13's MIT license, package metadata, changelog, contribution guide,
  and `py.typed` marker.

This change will not add a new SQL parser, database connector, authentication
system, secrets manager, or mandatory dependency. It will not claim that
application validation replaces a least-privilege database role.

## Validator Architecture

The validator will continue to parse with `sqlglot`, but security decisions
will use the parsed expression tree instead of raw keyword matching.

### Statement safety

The root statement must remain a single `SELECT`. The validator will also walk
the tree for write or administrative expression nodes so a write hidden inside
a CTE or nested construct cannot inherit the root `SELECT` classification.
String literals and comments are data, not operations, so words such as
`'drop'` inside them will not cause false positives.

### Relation and column resolution

The validator will build a relation map for each referenced table:

- the real table name maps to itself;
- each table alias maps back to the real table;
- qualified columns resolve through this map;
- an unqualified column resolves only when exactly one referenced schema table
  contains that column;
- zero matches produce an unknown-column error;
- multiple matches produce an ambiguous-column error.

This resolution feeds both schema validation and access-policy enforcement so
the two checks cannot disagree about what `c.email` refers to.

For stars, the validator will expand the exposure conceptually from the known
schema. An unqualified star covers every referenced table; `alias.*` covers the
resolved table. A star is rejected if any exposed table has blocked columns or
if an allowed-column policy does not include every exposed column. Existing
`SafetyPolicy.allow_select_star=False` remains the stricter global switch.

### Function policy

`SafetyPolicy` will gain:

- `blocked_functions`, seeded with dangerous PostgreSQL functions such as
  `nextval`, `setval`, `pg_terminate_backend`, `pg_cancel_backend`,
  `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, and `pg_sleep`;
- optional `allowed_functions`; when configured, every called SQL function must
  appear in the allowlist.

Names will be compared case-insensitively after dialect-normalized parsing.
Blocked functions win over allowed functions. A rejection is a high-severity
policy failure with an actionable function name.

## PostgreSQL Defense in Depth

`PostgresConnector.execute_readonly` will make `SET TRANSACTION READ ONLY` the
first command in the SQLAlchemy transaction, then apply `SET LOCAL
statement_timeout`, then execute the validated query. Tests will verify command
order and that the result path remains unchanged.

Documentation will state that deployments must still use a dedicated database
role with only the required `SELECT` and schema privileges. Read-only
transactions do not neutralize every privileged PostgreSQL function, which is
why function policy remains necessary.

## Compatibility and Error Handling

Public method and MCP tool names remain unchanged. Validation failures continue
to return structured `ValidationResult` data and MCP errors. New failures will
use explicit messages for blocked functions, unknown multi-table columns,
ambiguous columns, and star-based access-policy exposure.

Existing valid joins, aggregates, scalar functions, SQLite queries, row
filters, masking, audit records, and limit rewriting must remain compatible.

## Test Strategy

Implementation will follow red-green-refactor cycles. Regression tests will be
written and observed failing before production changes.

Required cases:

- blocked column through `SELECT *`;
- blocked column through a table alias;
- blocked unqualified column in a multi-table join;
- allowed-column policy through stars and aliases;
- unknown and ambiguous multi-table columns;
- harmless dangerous-looking words in literals and comments;
- each default dangerous PostgreSQL function category;
- optional function allowlist behavior;
- write nodes nested beneath a query when supported by the parser;
- PostgreSQL transaction-read-only and timeout command ordering;
- full unit suite, smoke eval, safety eval, and live MCP stdio probe.

## Acceptance Criteria

- Every reproduced bypass is rejected by an automated regression test.
- Harmless literals containing blocked-operation words validate successfully.
- PostgreSQL execution begins with a read-only transaction command.
- Existing public APIs and MCP schemas do not change.
- The complete test suite, smoke eval, safety eval, package build, and MCP probe
  pass from the isolated branch.
- The branch contains PR #13's open-source/package artifacts and introduces no
  new mandatory dependency.
