# Trace doctor: Foundry SQL in Claude Code (2026-07-17 to 2026-10-21)

Tools: `mcp__palantir-mcp__run_sql_query_on_foundry_dataset`. Period: 2026-07-17 to 2026-10-21.

- **5,163** query calls in **71** sessions (224 transcripts, including subagents)
- **609** failed: **11.8%** of the 5,158 that ran; 5 denied by the user or a permission rule
- Latency p50 **3.8s**, p90 **11.6s**, p99 80.8s, max 901s; **84** calls over 60s (6 over 5 min)

## Failures by class

| Class | Calls | % of calls | % of failures | Sessions | Fix first in |
|---|---:|---:|---:|---:|---|
| Unresolved column | 191 | 3.7% | 31% | 38 | tool |
| Result too large for context | 165 | 3.2% | 27% | 9 | tool |
| Upstream / network (no SQL error returned) | 107 | 2.1% | 18% | 22 | tool |
| Unknown table / dataset | 57 | 1.1% | 9% | 14 | tool |
| Data type mismatch | 47 | 0.9% | 8% | 19 | data |
| Syntax / parse | 20 | 0.4% | 3% | 8 | prompt |
| Other SQL analysis error | 13 | 0.3% | 2% | 9 | prompt |
| Invalid tool call | 8 | 0.2% | 1% | 7 | prompt |
| Timeout | 1 | 0.0% | 0% | 1 | tool |

## Retry chains

A chain starts at a failed query and follows the next queries on the same datasets in the same transcript until one succeeds (recovered) or the agent moves on (abandoned).

- 382 chains, 609 failed attempts; 260 recovered, 122 abandoned
- Failed attempts per chain: 1: 300, 2: 45, 3: 13, 4: 8, 5+: 16 (max 15)
- Time spent on failed attempts: 134 min; median time from first failure to recovery 17s
- Agent fetched a schema mid-chain in 40 chains
- First failure class: Unresolved column 152, Upstream / network (no SQL error returned) 74, Result too large for context 42, Data type mismatch 41, Unknown table / dataset 40, Syntax / parse 15, Other SQL analysis error 10, Invalid tool call 7, Timeout 1
- 91 successful queries were exact repeats of an earlier success in the session

## Latency

| Outcome | p50 (s) |
|---|---:|
| Succeeded | 3.8 |
| Unresolved column | 3.2 |
| Result too large for context | 22.1 |
| Upstream / network (no SQL error returned) | 2.5 |
| Unknown table / dataset | 2.1 |
| Data type mismatch | 3.7 |
| Syntax / parse | 0.6 |
| Other SQL analysis error | 3.6 |
| Invalid tool call | 1.3 |
| Timeout | 901.3 |

## Guard replay

The schema cache built from this history holds 924 datasets. The guard could check columns for 3,487 queries (every dataset cached at the time).

- Failures it would have stopped before execution: Unresolved column 98, Data type mismatch 24, Upstream / network (no SQL error returned) 13, Syntax / parse 6, Result too large for context 4. Outside the column and type classes, these queries failed for another reason first but also used a column the dataset lacks or did date arithmetic on a DATE column.
- Successful queries it would have blocked by mistake: 38 of 3,099 checked. These are mostly datasets rebuilt with new columns after their schema was cached; live, the agent resends the query once and the hook drops the stale entry.

Sample guard replies (redacted):

- Column 'product' does not exist; did you mean Product_Name?
- Column 'category_name' does not exist; did you mean Customer_Name?
- Column 'rank' does not exist.

## Findings

### 1. [tool fix] Validate columns before the query runs (QueryPilot guard) (191 calls)

191 queries referenced a column the dataset does not have. Replaying the history, the guard (schema cache built from earlier schema calls and SELECT * results) would have stopped 98 of them before they reached the warehouse, with a 'did you mean' reply, and wrongly blocked 38 of 3099 successful queries it could check. The rest touched datasets with no cached schema.

- Column 'product' does not exist; did you mean Product_Name?
- Column 'category_name' does not exist; did you mean Customer_Name?
- Column 'rank' does not exist.

**Fix:** querypilot hooks install --guard (optionally --require-schema, which makes the agent fetch a dataset's schema before its first query).

### 2. [tool fix] Cap result size before it floods the context window (165 calls)

165 results exceeded Claude Code's output limit. 141 had neither a LIMIT nor a rowLimit at or below the cap, and 122 used SELECT *. rowLimit on those calls: <= 50: 13, <= 100: 5, <= 200: 6, <= 500: 3, <= 1000: 137, not set: 1. Wide text columns overflow even at 100 rows.

**Fix:** querypilot hooks install --guard --max-rows 200 enforces a cap; the prompt fix is to select only the needed columns and aggregate in SQL instead of reading raw rows.

### 3. [tool fix] Transport and server errors reach the agent as opaque failures (107 calls)

107 calls failed without a SQL error: network fetch failures, Foundry internal errors, and HTTP errors whose body the MCP tool dropped (a stack trace instead of a message).

**Fix:** Fix in the tool layer: retry with backoff on network and 5xx errors and surface Foundry's error body. The agent cannot fix these by rewriting SQL.

### 4. [prompt fix] Tell the agent the real names of columns it keeps guessing (92 calls)

The same wrong column name failed repeatedly, often across sessions, so the agent is not carrying the schema forward. Recurring guesses:

- `termination_reason` x20 in 1 session (dataset_A, dataset_B); closest real columns: terminated_date, job_change_reason, pension_id
- `product` x9 in 3 sessions (dataset_E, dataset_F); closest real columns: product_id, Product_Name, periodicity
- `run_seconds` x5 in 1 session (dataset_G); closest real columns: raw_json, branch_name, build_status
- `evidence_grade` x4 in 1 session (dataset_H); closest real columns: tenure_years, hr_role_group, department
- `posting_key` x4 in 1 session (dataset_I); closest real columns: cell_key, state, title
- `record_id` x4 in 3 sessions (dataset_F, dataset_J); closest real columns: ERP_ID, Company_ID, Deal_ID
- `pct_days_used` x3 in 1 session (dataset_M, dataset_N); closest real columns: Repeat_Users, Churned_Users, Play_Count
- `name` x3 in 3 sessions (dataset_E, dataset_J); closest real columns: NAME, status_name, ACTIVE

**Fix:** Add the hints below to the agent's instructions (CLAUDE.md, or `hints:` in querypilot.yaml); when a guess recurs across many sessions, the data fix is a column description or an alias column in a view.

### 5. [tool fix] Identical queries re-run in the same session (91 calls)

91 successful queries repeated a query that had already succeeded earlier in the same session.

**Fix:** Cache results per session in the tool, or tell the agent to keep earlier results instead of re-querying.

### 6. [tool fix] Long-running queries stall the agent (84 calls)

84 calls took longer than 60s (6 over 5 minutes); 14 of those failed anyway. 1 calls failed with a timeout.

**Fix:** Cap rows and require a filter on the partition/date column for large datasets; for queries the agent repeats, materialize the result as a dataset or view (data fix).

### 7. [tool fix] Catch wrong or inaccessible dataset references (57 calls)

57 queries named a dataset that was not found or not readable (Foundry answers 403 for both a wrong RID and missing access). The guard caught 0 (malformed RIDs).

**Fix:** Keep a registry of the datasets the agent uses (RID, path, one-line purpose) in its instructions, and enable --require-schema so a typo fails at the cheap schema call.

### 8. [prompt fix] Fetch the schema before the first query, not after it fails (40 calls)

In 40 retry chains the agent fetched a dataset schema only after a query failed.

**Fix:** Instruct: 'Before querying a Foundry dataset for the first time in a session, call get_foundry_dataset_schema and use its exact column names.' The guard's --require-schema enforces this.

### 9. [prompt fix] Spark SQL dialect mistakes (33 calls)

33 queries failed to parse or failed Spark analysis (SCALAR_SUBQUERY_IS_IN_GROUP_BY_OR_AGGREGATE_FUNCTION x2, UNSUPPORTED_GENERATOR x2, INVALID_USAGE_OF_STAR_OR_REGEX x2, UNRESOLVED_ROUTINE x1, DIVIDE_BY_ZERO x1).

**Fix:** State the dialect in the agent's instructions: 'Foundry SQL is Spark SQL: quote dataset RIDs in backticks, every non-aggregated column must be in GROUP BY, use try_cast/try_divide.'

### 10. [data fix] Date columns read as epoch milliseconds (30 calls)

47 queries failed on a data type mismatch; 30 of them treated a DATE or TIMESTAMP column as epoch milliseconds (`col / 1000`, `col >= 1767225600000`). 327 successful results displayed a DATE/TIMESTAMP column as 13-digit numbers, which is where the agent learns the wrong type.

- `Report_Date` (DATE, dataset_F) x3
- `hired_date` (DATE, dataset_A) x3
- `Date` (DATE, dataset_T) x3
- `Close_Date` (DATE, dataset_K) x2
- `Activity_Date` (DATE, dataset_U) x2
- `start_date` (DATE, dataset_V) x2
- `Report_Date` (DATE, dataset_W) x1
- `start_date` (DATE, dataset_X) x1

**Fix:** Data: add column descriptions that state the type (suggested `descriptions:` below), or publish views with explicit DATE columns. Tool: the PostToolUse hook annotates results whose date columns are rendered as epoch ms, and the guard blocks `date_col / 1000`.

## Suggested prompt hints

```yaml
hints:
  - "In dataset_A, dataset_B, there is no `termination_reason` column; use `terminated_date`."
  - "In dataset_E, dataset_F, there is no `product` column; use `product_id`."
  - "In dataset_E, dataset_J, there is no `name` column; use `NAME`."
  - "Foundry SQL results show DATE/TIMESTAMP values as epoch-millisecond numbers, but the columns are real dates: never divide them by 1000 or compare them with 13-digit numbers; use DATE 'YYYY-MM-DD' literals and date functions."
  - "Foundry SQL is Spark SQL: quote dataset RIDs and paths in backticks, list every non-aggregated column in GROUP BY, and prefer try_cast / try_divide."
```

## Suggested column descriptions

```yaml
descriptions:
  "dataset_A.hired_date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. hired_date >= DATE '2026-01-01') or date functions."
  "dataset_AA.Created_At": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. Created_At >= DATE '2026-01-01') or date functions."
  "dataset_AB.date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. date >= DATE '2026-01-01') or date functions."
  "dataset_AC.start_date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. start_date >= DATE '2026-01-01') or date functions."
  "dataset_F.Report_Date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. Report_Date >= DATE '2026-01-01') or date functions."
  "dataset_K.Close_Date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. Close_Date >= DATE '2026-01-01') or date functions."
  "dataset_T.Date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. Date >= DATE '2026-01-01') or date functions."
  "dataset_U.Activity_Date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. Activity_Date >= DATE '2026-01-01') or date functions."
  "dataset_W.Report_Date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. Report_Date >= DATE '2026-01-01') or date functions."
  "dataset_X.start_date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. start_date >= DATE '2026-01-01') or date functions."
  "dataset_Y.release_date": "DATE. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. release_date >= DATE '2026-01-01') or date functions."
  "dataset_Z.transaction_datetime": "TIMESTAMP. Not epoch milliseconds, although query results display it as a 13-digit number; filter with DATE literals (e.g. transaction_datetime >= DATE '2026-01-01') or date functions."
```
