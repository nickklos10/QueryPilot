# Design note: `querypilot proxy`

Status: prototype. Postgres only.

## What it is

`querypilot proxy` is a Postgres wire-protocol server that sits between any Postgres client and the real database:

```
psql / psycopg / SQLAlchemy / MCP database server / BI tool
        │  Postgres protocol (unchanged client)
        ▼
querypilot proxy ── classify → validate/rewrite → READ ONLY txn → mask → audit
        │  libpq (one upstream session per client)
        ▼
real Postgres
```

The SDK protects agents that call QueryPilot. The proxy protects everything else, with no code change: point the client's connection string at the proxy. Existing MCP database servers inherit QueryPilot's enforcement this way, including the ones whose own read-only modes have been bypassed (see [research/developer-needs.md](research/developer-needs.md)).

## How a statement flows

1. **Terminate the client protocol.** The proxy is a real Postgres server to the client. Framing and message encoding come from [pygwire](https://github.com/DHUKK/pygwire), a sans-I/O codec that is pure Python and MIT-licensed. We chose it over [riffq](https://pypi.org/project/riffq/), which is Arrow-typed and a pre-release with few wheels, and over [buenavista](https://pypi.org/project/buenavista/), whose last release was in 2024. The proxy owns the protocol state: transactions, prepared statements, portals, and error recovery up to the next Sync.
2. **Classify** each statement (`proxy/statements.py`) into exactly one route:
   - **Session commands.** These are `BEGIN`/`COMMIT`/`ROLLBACK`/savepoints, `SET`/`RESET`/`SHOW` of an allowlisted setting, and `DEALLOCATE`. The proxy re-generates each one from its tokens and never forwards the client's text. `BEGIN` always becomes `BEGIN … READ ONLY`, and `READ WRITE` is rejected.
   - **Catalog queries.** These are SELECTs that read only `pg_catalog` / `information_schema` relations and call only allowlisted catalog functions. They are forwarded unchanged, which is what drivers, `psql \d`, and ORM reflection need. Relations that hold row data or other sessions' SQL (`pg_stats`, `pg_statistic*`, `pg_stat_*`, `pg_largeobject`, `pg_authid`, …) are denied. So is any function that takes a query string (`query_to_xml`, `ts_stat`, …) and any function from a schema other than `pg_catalog`.
   - **Data queries.** Everything else goes through `SQLValidator`, the same one the SDK uses. Only `ValidationResult.rewritten_sql` is sent upstream, so row filters wrap each table reference and the LIMIT is capped at `max_rows`.
   - **Anything else is rejected.** That covers COPY, PREPARE/EXECUTE, DO, EXPLAIN, LISTEN/NOTIFY, DISCARD, unknown SET names, and statements sqlglot cannot parse.
3. **Execute upstream.** Each data or catalog statement runs inside an explicit `BEGIN READ ONLY` transaction. In the extended protocol that transaction is implicit and lasts until Sync. In an explicit client transaction it is the client's own read-only transaction. The upstream session also has `default_transaction_read_only = on` and a `statement_timeout`, and the client cannot change either. The proxy works with raw libpq results (`psycopg.pq`), so values, type OIDs, type modifiers, and text or binary encodings reach the client unchanged.
4. **Mask by position.** `ValidationResult.output_masks` follows column lineage through aliases, expressions, CTEs and set operations. Each DataRow value is replaced in place: `redact` gives `[REDACTED]`, `null` gives NULL, and `hash` gives the SHA-256 of the value. A redacted or hashed column is described as `text` in RowDescription, with its source table hidden. If the row shape does not match the analyzed projection, every column gets the strictest mask, as in the SDK.
5. **Audit.** Every statement is written to the audit sink, executed or not. A record carries `operation = proxy_<route>`, the client SQL, the SQL actually sent, the validation result, the row count, the timing, and the client's user and application name. `querypilot proxy` writes JSONL, to `querypilot-proxy-audit.jsonl` by default.
6. **Reject like Postgres.** A rejection is an `ErrorResponse` with a meaningful SQLSTATE: `42703` unknown column, `42P01` unknown table, `25006` write attempt, `42501` policy. Its message is the validator's agent-safe message, and its `HINT` is the SDK's "did you mean" repair hint. An agent therefore sees an ordinary database error that it can act on. Upstream errors are passed through with stored values redacted.

## Why it fails closed

The proxy sits in front of databases that agents can reach with prompt-injected SQL. The question for every message is "is this known safe?", not "is this known bad?":

- The routes are allowlists. A statement that fits none of them is rejected, and so is a SET of an unlisted setting or a catalog function not on the list. A new driver feature therefore produces a clear error instead of an unchecked query. The cost is that the allowlists need to grow as clients are tested (`psql \d*` and SQLAlchemy reflection are covered today).
- Session commands are re-generated, not forwarded, so nothing the client typed after `SET name =` reaches the database.
- `search_path` and `role` cannot be set. Changing them would let an unqualified table name resolve to a table the policy never saw. For the same reason, schema-qualified names must belong to the introspected schemas.
- A parse failure, an analysis failure, or a mask that cannot be computed means rejection, never pass-through.
- Read-only is enforced three times: by the classifier, by the explicit `READ ONLY` transaction, and by the session default. A bypass would have to defeat all three.
- Startup parameters such as `options=-c …` are ignored, and only allowlisted formatting settings are applied. The client never chooses the upstream user, database or credentials. The proxy owns them, so an agent never holds the real database password.
- Off loopback, the proxy refuses to start without a password.

## Protocol coverage

| Area | Status |
|---|---|
| Startup: protocol 3.0, `NegotiateProtocolVersion` for 3.2 and `_pq_` options | supported |
| SSLRequest / GSSENCRequest | answered `N` (no TLS termination yet) |
| Authentication | trust on loopback, or a cleartext password (`--password`) |
| Simple query, including several statements in one message | supported; each statement commits on its own |
| Extended query: Parse/Bind/Describe/Execute/Close/Sync/Flush | supported, with named statements and portals |
| `Execute` with `max_rows` and `PortalSuspended` | supported (results are buffered, capped by `max_rows`) |
| Text and binary parameters and results | supported, passed through as raw bytes |
| Mixed per-column result formats in one Bind | rejected (`0A000`) |
| Transactions, savepoints, isolation levels, `25P02` after errors | supported, always READ ONLY |
| CancelRequest | supported (forwarded to the upstream session) |
| ParameterStatus after SET | supported |
| COPY, FunctionCall, replication, LISTEN/NOTIFY | rejected |

Tested live against Postgres 16 with psycopg 3 (simple, extended, prepared, binary and autocommit), psql (queries and `\d`, `\dt`, `\di`, `\dn`, `\df`, `\du`, `\dx`), SQLAlchemy 2 reflection, and a raw extended-protocol client that checks portal suspension. Not yet tested: node-postgres clients (such as the MCP reference server), JDBC (DBeaver, Metabase) and asyncpg. asyncpg requests binary results and runs type introspection queries, so the catalog allowlist may need to grow for it.

## Known gaps

- **TLS**: there is no TLS termination, and clients with `sslmode=require` cannot connect. Bind to loopback, or put the proxy behind a TLS terminator.
- **Auth**: only cleartext or trust. SCRAM is not implemented, and there is no per-user policy. Every client shares one policy and the one upstream role.
- **Not supported yet**: server-side cursors (`DECLARE … CURSOR`, used by psycopg named cursors), `EXPLAIN`, `COPY … TO STDOUT` exports, `DISCARD ALL` from poolers, and SQL-level `PREPARE`.
- **`LIMIT $1`**: the validator replaces a non-literal LIMIT with the cap. The bound parameter then has no placeholder, and Postgres rejects the Bind.
- **Function bodies**: user-defined functions that the validator does not block run with their own privileges. Restrict them with `allowed_functions` in the safety policy, and connect upstream with a role that cannot read more than the policy allows.
- **Catalog metadata**: catalog access exposes object names, though not rows, for everything the upstream role can see, including tables and columns the policy hides. Use a least-privileged upstream role, or `--no-catalog`, which breaks `psql \d` and ORM reflection.
- **Hashes**: `hash` masks hash the value as Postgres encoded it. Text results match the SDK's hashes, but binary results hash the binary encoding.
- **Implicit transactions**: a multi-statement simple query runs as separate implicit transactions rather than one, as Postgres would do.
- **Limits**: the schema is introspected once at startup, and there is one upstream connection per client, with no pooling.
- **Other databases**: Postgres only. MySQL's protocol would need its own front end, though classification and masking would carry over.
