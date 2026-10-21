# What developers need from agent-SQL tooling

**Date:** 2026-10-20

**Summary.** The most widely reported problem is that LLM-generated SQL runs against real databases with read-only protection that does not hold. CVEs and shipped bypasses appear across text-to-SQL apps, agent frameworks and MCP database servers. The next tier covers getting SQL right: hallucinated identifiers, large schemas, wrong dialects, and missing domain knowledge. The research literature measures several of these with concrete gains, such as +4.63 EX from an execution-feedback repair loop and +15 to +20 EX from domain "evidence" hints on BIRD. Operational needs follow: per-user access control, explicitly bounded results, painless install, framework adapters (the LangChain SQL toolkit no longer has an owner), connectivity, and cost guards.

**Method.** Four parallel research passes used the Firecrawl developer index (GitHub issues, PRs, READMEs and docs), developer web search, targeted scrapes of issue lists sorted by reactions, and paper search for benchmark and ablation results. The passes covered:

1. Text-to-SQL apps: Vanna, WrenAI, DB-GPT, Defog sqlcoder.
2. Agent frameworks: LangChain/LangGraph SQL toolkit, LlamaIndex NL2SQL, OpenAI Agents SDK, CrewAI, Claude Agent SDK.
3. MCP database servers and MCP client setup.
4. SQL quality research: Spider/BIRD, schema linking, repair, sqlglot, eval tooling.

About 430 sources were seen in total. Every link in this document appeared in a search or scrape result during that work. No links were constructed by hand, and every number is copied from the page or abstract it cites.

---

## Method and evidence strength

Each need is rated by how widely it was observed:

| Strength | Definition |
|---|---|
| **Strong** | Seen across 3 or more independent projects, or backed by many duplicate or high-reaction issues (top-reacted issues, CVEs, repeated re-filings). |
| **Medium** | Seen in 2 projects, or in a few issues with some engagement. Also used when a theme spans 3 or more projects but is mostly demand inferred from vendor work or single low-engagement reports. The row says so when this applies. |
| **Weak** | A single source (one blog, forum post or issue). |

Notes on the counts and signals:

- **"# projects seen in"** counts distinct projects or products that appear in the notes with an issue, PR, CVE, advisory or documentation item on the theme. Benchmark papers are not counted as projects. They are cited separately as measured evidence. The project list for each count is under "Seen in" in each section.
- **Reaction ranks** ("top-N by reactions") come from scraping each repository's `issues?q=is:issue sort:reactions-desc` page on 2026-10-20. Exact reaction counts were mostly not visible. Comment counts are reported where they were.
- **Measured numbers** (EX = execution accuracy, in percentage points unless noted) come from paper tables or abstracts. Ablation deltas are specific to the benchmark, model and system they were measured on, so they are not directly comparable to each other.
- **Ranking** is by cross-source frequency (how many of the four research passes found the theme and how many projects it appears in), then by strength of the individual signals.

---

## Top needs

| Rank | Need | Strength | # projects seen in | Representative evidence |
|---|---|---|---|---|
| 1 | Read-only enforcement that holds, with safe defaults | Strong | 21 | [Datadog: Postgres MCP read-only escape](https://securitylabs.datadoghq.com/articles/mcp-vulnerability-case-study-SQL-injection-in-the-postgresql-mcp-server/), [DB-GPT #2963](https://github.com/eosphoros-ai/db-gpt/issues/2963), [CrewAI #5311](https://github.com/crewaiinc/crewai/issues/5311) |
| 2 | Catalog-grounded validation with actionable errors | Strong | 8 | [sqlcoder #75](https://github.com/defog-ai/sqlcoder/issues/75), [langchain-community #294](https://github.com/langchain-ai/langchain-community/issues/294), [sqlglot #8652](https://github.com/tobymao/sqlglot/issues/8652) |
| 3 | Large-schema handling (linking, pruning, token budgets) | Strong | 13 | [WrenAI #1946](https://github.com/canner/wrenai/issues/1946), [DB-GPT #3045](https://github.com/eosphoros-ai/db-gpt/issues/3045), [DBHub search_objects](https://github.com/bytebase/dbhub/blob/2dbf01af53e34784eafc00d777394ec844b98eca/docs/tools/search-objects.mdx) |
| 4 | Dialect correctness (generate, validate, transpile per target) | Strong | 9 | [Vanna #310](https://github.com/vanna-ai/vanna/issues/310), [CrewAI #7782](https://github.com/crewaiinc/crewai/issues/7782), [Snowflake-Labs #174](https://github.com/snowflake-labs/mcp/issues/174) |
| 5 | Access control and data governance (row/tenant scoping, allowlists, PII) | Strong | 16 | [LangChain #11243](https://github.com/langchain-ai/langchain/issues/11243), [Supabase MCP leak](https://simonwillison.net/2025/Jul/6/supabase-mcp-lethal-trifecta/), [WrenAI #1994](https://github.com/canner/wrenai/issues/1994) |
| 6 | Repair loop that converges (classified errors, bounded retries, clean extraction) | Strong | 6 | [Vanna #279](https://github.com/vanna-ai/vanna/issues/279), [LangGraph #6731](https://github.com/langchain-ai/langgraph/issues/6731), [MAC-SQL ablation](https://arxiv.org/html/2312.11242) |
| 7 | Schema semantics and domain knowledge (descriptions, glossary, sample values) | Strong | 7 | [WrenAI #1120](https://github.com/canner/wrenai/issues/1120), [LlamaIndex #17479](https://github.com/run-llama/llama_index/issues/17479), [BIRD paper](https://arxiv.org/html/2305.03111) |
| 8 | Bounded results with explicit truncation | Strong | 13 | [Metabase #82850](https://github.com/metabase/metabase/issues/82850), [DBHub #404](https://github.com/bytebase/dbhub/issues/404), [LangChain #6836](https://github.com/langchain-ai/langchain/issues/6836) |
| 9 | Painless install and client setup | Strong | 14 | [Vanna #917](https://github.com/vanna-ai/vanna/issues/917), [DB-GPT #2561](https://github.com/eosphoros-ai/DB-GPT/issues/2561), [python-sdk #478](https://github.com/modelcontextprotocol/python-sdk/issues/478) |
| 10 | Framework adapters and MCP exposure | Strong | 9 | [Vanna #721](https://github.com/vanna-ai/vanna/issues/721), [langchain-community #674](https://github.com/langchain-ai/langchain-community/issues/674), [WrenAI #2337](https://github.com/canner/wrenai/issues/2337) |
| 11 | Connectivity and connector breadth (multi-DB, schemas, views, SSL/SSH) | Strong | 12 | [WrenAI #173](https://github.com/Canner/WrenAI/issues/173), [langchain-community #285](https://github.com/langchain-ai/langchain-community/issues/285), [DBHub #293](https://github.com/bytebase/dbhub/issues/293) |
| 12 | Cost, latency and timeout guards | Medium | 12 | [DB-GPT #83](https://github.com/eosphoros-ai/DB-GPT/issues/83), [Lightdash AI agent limits](https://github.com/lightdash/mintlify-docs/blob/9f3dd17e4ff1c2100e85567ba434c5881440d128/integrations/connect-project.mdx), [gbrain #6383](https://github.com/garrytan/gbrain/issues/6383) |
| 13 | Broad LLM provider support | Strong | 3 | [Vanna #1104](https://github.com/vanna-ai/vanna/issues/1104), [WrenAI #1777](https://github.com/canner/wrenai/issues/1777), [DB-GPT #2352](https://github.com/eosphoros-ai/db-gpt/issues/2352) |
| 14 | Eval and regression testing with a trustworthy comparator | Medium (demand); strong (tooling defects) | 12 | [WrenAI #355](https://github.com/canner/wrenai/issues/355), [mini_dev #41](https://github.com/bird-bench/mini_dev/issues/41), [ModularSQL multiplicity](https://arxiv.org/html/2609.29573v1) |
| 15 | Transparency, approval and audit (show SQL, SQL-only mode, HITL) | Medium | 6 | [SO: retrieve generated SQL](https://stackoverflow.com/questions/77953302/how-to-retrieve-the-generated-sql-query-from-create-sql-agent-of-langchain), [LlamaIndex #9422](https://github.com/run-llama/llama_index/issues/9422), [OpenAI Agents guardrails](https://github.com/openai/openai-agents-python/blob/64cd5dfc5a1579870dcbed0ea42b27aa84033fee/docs/guardrails.md) |

Lower-signal themes that are not ranked are listed after need 15.

---

## 1. Read-only enforcement that holds, with safe defaults

**What breaks.** LLM-generated SQL is executed with no read-only gate in Vanna, DB-GPT and the WrenAI CLI. In the WrenAI CLI case, DDL and DML ran against a connected MySQL 8 database. This has produced many CVEs and advisories, including CVE-2024-5565, CVE-2024-7764, CVE-2024-10835, CVE-2025-51458 and CVE-2026-4505 (apps), and CVE-2023-36189 and CVE-2024-23751 (LangChain, LlamaIndex). Fixes tend to arrive one layer at a time. DB-GPT's CVE fix filtered user SQL but still treated LLM-output SQL as trusted (#2963).

Every common guard technique has been bypassed in a shipped MCP server:

- `BEGIN READ ONLY` is escaped by `COMMIT; DROP SCHEMA public CASCADE;` (the archived reference Postgres server).
- An AST allowlist missed functions in the `FROM` clause (`pg_read_file`, Postgres MCP Pro #178).
- ClickHouse `readonly=1` was not enforced when the user profile allowed writes.
- SQLite `mode=ro` does not stop `VACUUM INTO` or `ATTACH`.

Helper tools are injection surfaces as well, for example a `count_rows` `where` parameter and an f-string in `describe_table`.

**What developers ask for.**

- AST-based default-deny, with single-statement execution.
- Rejection of writable CTEs, `SELECT INTO` and `EXPLAIN ANALYZE <write>`.
- Validation of the *final* SQL after any rewrite or expansion.
- Dialect-specific blocklists for dangerous functions (DuckDB file I/O, Oracle `DBMS_*`, Postgres `pg_read_file`/`COPY TO PROGRAM`/`lo_export`/`dblink`).
- Engine-native read-only plus a least-privilege role, with an honest statement of what read-only does not cover.
- Read-only as the default, opt-in write modes beyond a binary switch (DML-only, SHOW/DESCRIBE allowed, per-statement-type allowlists), and no false blocks on legitimate dialect syntax.

**Seen in (21):**

- Apps: Vanna, WrenAI, DB-GPT.
- Frameworks and agents: LangChain, LlamaIndex, CrewAI, OpenAgents, Langchain-Chatchat, NVIDIA NeMo Agent Toolkit.
- MCP servers: MCP reference Postgres server, MCP reference SQLite server, Postgres MCP Pro, mcp-clickhouse, Xata MCP, pgEdge MCP, MCP Toolbox, DBHub, MotherDuck MCP, Supabase MCP, Snowflake-Labs MCP, MCP Alchemy.

**Evidence**

- [Vanna #1078](https://github.com/vanna-ai/vanna/issues/1078): RCE via prompt injection to arbitrary SQL to Oracle command execution. Vanna "executes all SQL statements generated by the LLM without filtration". Duplicate theme in #1062.
- [WrenAI #2679](https://github.com/canner/wrenai/issues/2679): merged security fix. The CLI ran DDL/DML. The fix adds always-on read-only validation that runs after model/view expansion.
- [DB-GPT #2963](https://github.com/eosphoros-ai/db-gpt/issues/2963): bypass of an earlier CVE fix. LLM-output SQL "is considered trusted and execute directly".
- [LangChain #5923](https://github.com/langchain-ai/langchain/issues/5923): asks for a hook to intercept and review SQL before it is sent. 7 comments. Resolved by moving the code to `langchain-experimental`, not by adding a guard.
- [CVE-2024-23751 (SentinelOne)](https://www.sentinelone.com/vulnerability-database/cve-2024-23751/): LlamaIndex `NLSQLTableQueryEngine` SQL injection via text-to-SQL.
- [CrewAI #5311](https://github.com/crewaiinc/crewai/issues/5311): NL2SQLTool made read-only by default. Blocks DML, multi-statement input, writable CTEs and `EXPLAIN ANALYZE <write>`. Adds an `allow_dml` escape hatch. Breaking change.
- [Datadog Security Labs: Postgres MCP SQL injection](https://securitylabs.datadoghq.com/articles/mcp-vulnerability-case-study-SQL-injection-in-the-postgresql-mcp-server/): the reference server's read-only transaction is escaped with `COMMIT; DROP SCHEMA public CASCADE;`. Widely cited.
- [Postgres MCP Pro #178](https://github.com/crystaldba/postgres-mcp/issues/178): restricted-mode bypass with `SELECT * FROM pg_read_file('/etc/passwd')`. The AST allowlist checked FuncCall but not RangeFunction. Covered in the press.
- [mcp-clickhouse #131](https://github.com/clickhouse/mcp-clickhouse/issues/131): `run_select_query` executes DROP/INSERT/CREATE despite the readonly default. Official vendor server.
- [DBHub #344](https://github.com/bytebase/dbhub/issues/344): engine-level read-only, plus a documented warning that read-only cannot stop `pg_read_file`, `lo_export`, `dblink`, `COPY TO PROGRAM` or MySQL implicit-commit DDL.
- [cli-printing-press read-only bypass postmortem](https://github.com/mvanhorn/cli-printing-press/blob/46d42b58e5b700c7234a15a7cdf13d5f671d74b0/docs/solutions/security-issues/mcp-sql-search-readonly-bypass-2026-05-08.md): SQLite `mode=ro` does not stop `VACUUM INTO`/`ATTACH`. Includes comment-prefix bypasses and a 29-case attack test suite.
- [MotherDuck #38](https://github.com/motherduckdb/mcp-server-motherduck/issues/38): "Make read-only access the default" ("many LLMs will readily drop tables"). Shipped in v1.0 as a breaking change.

---

## 2. Catalog-grounded validation with actionable errors

**What breaks.** Generated SQL references tables or columns that do not exist, or ignores the columns supplied in context. This is worse when tables share similar column names, with smaller or local models, and when identifier casing differs from the catalog. Benchmark error analyses put schema-linking errors at 41.6% of ChatGPT failures on BIRD, 27.6% of failures on Spider 2.0 and about 32.5% for plain few-shot on Spider (DIN-SQL).

The LangChain toolkit's `sql_db_query_checker` is itself an LLM call, which developers find opaque. MindsDB replaced it with a parser-based check. sqlglot, the usual parser choice, is "a transpiler, not a validator". It parses some invalid SQL. Its `qualify` step does not validate columns in DML. It reports ambiguous columns as "could not be resolved". Its identifier case normalization is the top source of false "could not be resolved" errors. It also raises `OptimizeError` on some valid constructs. Separately, Vanna auto-saved SQL that ran but was wrong into tool memory, which reinforced the error.

**What developers ask for.**

- Resolve every table and column against the introspected catalog before execution, with dialect-correct casing and quoting.
- Report all unknown identifiers at once, not just the first.
- Return "unknown column X in table T, did you mean Y" feedback to the repair loop.
- Treat known parser gaps as soft warnings rather than hard rejects.
- A deterministic schema-hallucination metric for evals.

**Seen in (8):** Vanna, sqlcoder, WrenAI, LangChain, LlamaIndex, MindsDB, Ragas, sqlglot. Benchmark evidence: BIRD, Spider 2.0, DIN-SQL.

**Evidence**

- [Vanna #187](https://github.com/vanna-ai/vanna/issues/187): columns passed in RAG context are ignored in generated SQL. Long thread, also cross-referenced as discussion #194.
- [sqlcoder #75](https://github.com/defog-ai/sqlcoder/issues/75): column-name hallucination when tables have similar names. Top-25 by reactions, 7 comments.
- [sqlcoder #17](https://github.com/defog-ai/sqlcoder/issues/17): output does not keep table and column names in the provided casing. Top-5 by reactions.
- [WrenAI #2310](https://github.com/canner/wrenai/issues/2310): honor identifier case across policy/extract/CTE. BigQuery/DuckDB/Oracle case-folding produced empty CTEs.
- [Vanna #1103](https://github.com/vanna-ai/vanna/issues/1103): wrong SQL auto-saved to tool memory without validation ("Poisoned Memory").
- [LangChain blog: Query Construction](https://www.langchain.com/blog/query-construction): hallucination of fictitious tables and fields is listed as challenge #1.
- [langchain-community #294](https://github.com/langchain-ai/langchain-community/issues/294): `sql_db_list_tables` returns names only, so the agent picks `Playlist` instead of `PlaylistTrack`. Marked "still relevant", then closed by the sunset.
- [MindsDB #9411](https://github.com/mindsdb/mindsdb/issues/9411): a parser-based validation tool, separate from LangChain's LLM-based checker, that asks for regeneration when parsing fails.
- [BIRD paper](https://arxiv.org/html/2305.03111): ChatGPT failure analysis found wrong schema linking in 41.6% of failures and misunderstood database content in 40.8%.
- [sqlglot #8652](https://github.com/tobymao/sqlglot/issues/8652): `qualify` leaves DML columns unqualified, so `validate_qualify_columns` accepts columns that do not exist.
- [sqlglot #2760](https://github.com/tobymao/sqlglot/issues/2760): case sensitivity and identifier normalization cause false "could not be resolved" errors. Related: [#7919](https://github.com/tobymao/sqlglot/issues/7919).
- [Ragas #2644](https://github.com/vibrantlabsai/ragas/issues/2644): request for a deterministic `SQLSchemaHallucination` metric (valid references / total references). Hallucinated columns are common on schemas with 400+ columns.

---

## 3. Large-schema handling (linking, pruning, token budgets)

**What breaks.** Real databases have hundreds or thousands of tables and very wide tables. Prompts overflow, and so do embedding limits (16k context, a Vertex 20k embedding limit, 10,000+ token DDL). Top-K retrieval drops needed tables, and the model is not told that the list it sees is partial. In DB-GPT, an MSSQL database with 166 tables was described as having "10 tables".

Eager schema reflection is slow: `SQLDatabase.from_uri` took over 5 minutes on a 200+ table database, and a BigQuery dataset with about 50,000 tables never synced. On the MCP side, full schema dumps and large tool schemas use up the client's token budget. Leading servers have moved to progressive disclosure: names first, then detail on demand.

The measured evidence is two-sided:

- When the schema fits in context, skipping schema linking gives higher accuracy. The full pipeline scores 67.35, against 62.58 with table-to-column linking and 55.78 with single-column linking. Linkers drop needed columns 9.6-22.6% of the time.
- On larger schemas, pruning helps: MAC-SQL selector +2.11 EX, DIN-SQL schema linking +4.0 EX, and CHESS reports 5x fewer tokens.
- Enterprise-scale schemas (Spider 2.0) bring the best agents down to about 21-24%, against 86-91% on Spider 1.0.

**What developers ask for.**

- Pruning only above a token threshold, with foreign-key join paths kept connected.
- Wide-table column chunking.
- An explicit "partial schema" marker in prompts.
- Lazy, cached introspection.
- Search-then-describe tools with detail levels.

**Seen in (13):** Vanna, WrenAI, DB-GPT, sqlcoder, LangChain, LlamaIndex, Dust, DBHub, pgEdge MCP, Supabase MCP, MCP Alchemy, Cursor (client), GitHub Copilot CLI (client). Benchmark evidence: Spider 2.0, "Death of Schema Linking?", MAC-SQL, DIN-SQL, CHESS, DBCC.

**Evidence**

- [WrenAI #1946](https://github.com/canner/wrenai/issues/1946): MSSQL with 900+ tables. The schema retrieval JSON is too long and SQL quality is poor.
- [DB-GPT #3045](https://github.com/eosphoros-ai/db-gpt/issues/3045): tell the LLM that the table list is a top-K subset (166 tables, answered "10 tables"). Fixed in v0.8.1.
- [sqlcoder #9](https://github.com/defog-ai/sqlcoder/issues/9): real DDL is 10,000+ tokens. Top-4 by reactions, 5 comments.
- [Vanna #868](https://github.com/vanna-ai/vanna/issues/868): the vector store is not used properly and all training data is sent into the prompt. Bug label, top-25 by reactions.
- [WrenAI #1243](https://github.com/canner/wrenai/issues/1243): add reranker models to improve similarity search. Top-25 by reactions.
- [LangChain #3645](https://github.com/langchain-ai/langchain/issues/3645): `SQLDatabase.from_uri` takes more than 5 minutes on a 200+ table database. Several "same problem" replies. Workaround: [PR #18742](https://github.com/langchain-ai/langchain/issues/18742) `lazy_table_reflection`.
- [Dust #11579](https://github.com/dust-tt/dust/issues/11579): a BigQuery dataset with about 50,000 tables never syncs. The fix skips datasets with more than 1,000 tables.
- [DBHub `search_objects`](https://github.com/bytebase/dbhub/blob/2dbf01af53e34784eafc00d777394ec844b98eca/docs/tools/search-objects.mdx): `detail_level` names/summary/full, with a claimed 90-99% token reduction. Two default tools at about 1.4k tokens.
- [pgEdge: lessons learned writing a Postgres MCP server](https://www.pgedge.com/blog/lessons-learned-writing-an-mcp-server-for-postgresql): switches to summary mode automatically above 10 tables. Independently arrived at the same design.
- [Death of Schema Linking?](https://arxiv.org/html/2408.07702): when the schema fits, the full pipeline scores 67.35 against 62.58 and 55.78 with linking. Linker recall is 77.4-90.4%.
- [Spider 2.0](https://arxiv.org/html/2411.07763v2): enterprise schemas (755+ columns) collapse accuracy to about 21-24%. Wrong schema linking is 27.6% of failures.
- [DBCC context compression](https://arxiv.org/abs/2606.28601): 2.6M tokens reduced to 34.7K tokens on the largest Spider 2.0-Snow database. Linking recall rises from 0% to 56.5%, worth +1.8 to +1.9 EX.

---

## 4. Dialect correctness (generate, validate, transpile per target)

**What breaks.** Models default to Postgres or SQLite syntax. Examples are `LIMIT` on SQL Server, and `ILIKE` or `DATE_TRUNC` on SQLite. The Vanna MSSQL `LIMIT`/`TOP` issue was filed three times. Defog states that sqlcoder was "trained to output postgres queries only".

The tools themselves mis-map dialects. WrenAI reported "Unknown dialect 'mssql'", and the LangChain JS toolkit hard-coded "sqlite". Single-line `LIMIT` wrapping is a `ParseError` in Oracle, Trino and Redshift. Dialect-blind validators also produce false blocks, for example Snowflake VARIANT `data:field` classified as "Unknown".

On Spider 2.0, misuse of dialect functions accounts for 10.3% of failures. Supplying oracle function docs gave only a slight gain. sqlglot transpilation is lossy across dialects: 56.8% pass for Snowflake to DuckDB and 77.9% for BigQuery to DuckDB. Under the default `unsupported_level=WARN`, `TO_CHAR(d,'YYYY-MM')` silently becomes `CAST(d AS TEXT)`.

**What developers ask for.**

- Generate for the declared dialect.
- Parse and validate with the target dialect (`read=dialect`).
- Transpile when the model emits the wrong dialect.
- Feed dialect-specific hints into the repair loop.
- Raise, rather than log, when a transpile loses information.

**Seen in (9):** Vanna, WrenAI, sqlcoder, DB-GPT, LangChain (Python and JS), CrewAI, Snowflake-Labs MCP, Neon MCP, sqlglot.

**Evidence**

- [Vanna #310](https://github.com/vanna-ai/vanna/issues/310): always `LIMIT` instead of `TOP` on MSSQL, even with trained examples. Re-filed as [#334](https://github.com/vanna-ai/vanna/issues/334) and [discussion #368](https://github.com/vanna-ai/vanna/discussions/368).
- [sqlcoder-7b-2 discussion #18](https://huggingface.co/defog/sqlcoder-7b-2/discussions/18): output targets PostgreSQL even for SQL Server. The vendor confirms the model is "trained to output postgres queries only".
- [WrenAI #581](https://github.com/canner/wrenai/issues/581): "Unknown dialect 'mssql'. Did you mean mysql?" Dialect bug on the engine side.
- [WrenAI #2736](https://github.com/canner/wrenai/issues/2736): single-line `LIMIT` wrap is a `ParseError` in Oracle/Trino/Redshift. The fix was verified with sqlglot.
- [LangChain JS #1747](https://github.com/langchain-ai/langchainjs/issues/1747): the SQL Agent Toolkit ignores the MSSQL dialect.
- [LangChain JS #4184](https://github.com/langchain-ai/langchainjs/issues/4184): the SQL toolkit's database type is hard-coded to "sqlite".
- [CrewAI #7782](https://github.com/crewaiinc/crewai/issues/7782): NL2SQLTool emits PostgreSQL syntax (`ILIKE`, `DATE_TRUNC`) on SQLite.
- [LangChain #3305](https://github.com/langchain-ai/langchain/issues/3305): quotation marks around SQL. "Mixed case columns is a problem on OracleDB".
- [Snowflake-Labs MCP #174](https://github.com/snowflake-labs/mcp/issues/174): VARIANT `data:field` classified as "Unknown" and blocked. The fix, [PR #170](https://github.com/snowflake-labs/mcp/issues/170), has +11 reactions.
- [Spider 2.0](https://arxiv.org/html/2411.07763v2): dialect function usage is 10.3% of failures. Oracle function docs gave only a slight gain.
- [sqlglot #8088](https://github.com/tobymao/sqlglot/issues/8088): under `unsupported_level=WARN`, a Snowflake to DuckDB `TO_CHAR` silently becomes `CAST(d AS TEXT)`. The integration pass rates are in [PR #7499](https://github.com/tobymao/sqlglot/issues/7499) (Snowflake to DuckDB 56.8%, BigQuery to DuckDB 77.9%).

---

## 5. Access control and data governance (row/tenant scoping, allowlists, PII)

**What breaks.** Developers cannot restrict a user to their own rows. A 19-comment LangChain issue asking to fence users by `store_id` was closed with no feature added. Table filtering in several tools only changes which schema the LLM sees and is not enforcement. Solace documents "Table filtering is not access control". Agno's `schema` parameter "does not block cross-schema SQL". In paradigmxyz/centaur, `_ALLOWED_TABLES` was defined but never checked.

Read-only does not prevent exfiltration either. Supabase MCP's `service_role` bypassed RLS, and a prompt injection planted in a support ticket could read the whole database. Supabase's own response states that "even in read-only mode, prompt injection remains the number one concern". Policies are also applied unevenly: Snowflake-Labs semantic view tools bypassed the permission middleware.

**What developers ask for.**

- Row- and column-level security (WrenAI #1994 and #1934, impersonation in #2693, a pluggable `AccessControlProvider` in #2749).
- Multi-tenant isolation (Vanna #731, top-10 by reactions).
- Tenant predicates injected by rewriting the SQL.
- A mode where row data never reaches the LLM (`allow_llm_to_see_data`).
- PII masking that is policy-driven and cannot be turned off by the agent (redact, hash, partial, drop), with deterministic hashing so `GROUP BY` still works.
- A least-privilege role check at startup, and audit logs.

**Seen in (16):**

- Apps: WrenAI, Vanna.
- Frameworks and agents: LangChain, LlamaIndex, Solace Agent Mesh, Agno, paradigmxyz/centaur, Lightdash, PostHog.
- MCP servers: Supabase MCP, Snowflake-Labs MCP, MotherDuck MCP, MCP Toolbox, mcp-server-mysql (benborla), freepeak db-mcp-server, SAP Datasphere MCP.

**Evidence**

- [LangChain #11243](https://github.com/langchain-ai/langchain/issues/11243): no way to restrict a user to their data (`store_id`). Asks to intercept or modify the generated SQL. 19 comments. Closed without a feature.
- [Solace SQL tool README](https://github.com/solacelabs/solace-agent-mesh-core-plugins/blob/7599710a38318a336c47cf605e4774d4add24d8e/sam-sql-database-tool/README.md): "Table filtering is not access control".
- [paradigmxyz/centaur #186](https://github.com/paradigmxyz/centaur/issues/186): `_ALLOWED_TABLES` defined but never checked. The agent could read any table.
- [Simon Willison: Supabase MCP can leak your entire SQL database](https://simonwillison.net/2025/Jul/6/supabase-mcp-lethal-trifecta/): `service_role` bypasses RLS, combined with support-ticket prompt injection. The widely cited "lethal trifecta" incident.
- [Supabase: Defense in Depth for MCP Servers](https://github.com/supabase/supabase/blob/40943e51a9085a8c86a5c8b6dd9c7e9e3742b442/apps/www/_blog/2025-09-16-defense-in-depth-mcp.mdx): vendor response. Prompt injection remains the top concern even in read-only mode.
- [Snowflake-Labs MCP #166](https://github.com/snowflake-labs/mcp/issues/166): semantic view tools bypass the permission middleware.
- [WrenAI #1994](https://github.com/canner/wrenai/issues/1994): feature request for full access control including RLS and CLS. Related: shipped MDL RLS/CLS in [#1934](https://github.com/canner/wrenai/issues/1934).
- [Vanna #731](https://github.com/vanna-ai/vanna/issues/731): multi-tenant support. Top-10 by reactions.
- [Vanna #219](https://github.com/vanna-ai/vanna/issues/219): new `allow_llm_to_see_data` parameter (default False), a privacy control.
- [LlamaIndex #7457](https://github.com/run-llama/llama_index/issues/7457): mask PII in the query text and SQL result before synthesis, then unmask. Feature request with a design.
- [MCP Toolbox #3373](https://github.com/googleapis/mcp-toolbox/issues/3373): policy-based PII detection and dynamic masking (GDPR/CCPA/HIPAA, role-based levels, audit logs).
- [freepeak/db-mcp-server](https://github.com/freepeak/db-mcp-server): per-database `mask_pii` that the agent cannot turn off, `max_rows` with a `[Truncated]` notice, and `query_timeout`. An example of guardrails locked by the operator.

---

## 6. Repair loop that converges (classified errors, bounded retries, clean extraction)

**What breaks.** Feeding the database error back to the LLM is standard practice and an often-requested feature. Naive retries without new information return the same bad output: a 5-try retry in Vanna always produced the same answer. ReAct-style SQL agents fail on output parsing (`OutputParserException`), concatenate or invent tool names, and loop until they hit the recursion limit with small variations of the same error. Models wrap SQL in markdown fences, add a `sql` prefix or add prose, and the database rejects it. Errors are also swallowed or reported to the agent as success.

The research evidence shows that a repair loop is one of the largest single levers. It also shows that repair applied indiscriminately, or with the wrong wording, breaks correct queries:

- Removing revision costs 6.80 EX in CHESS, and a single pass instead of several costs 3.40.
- The MAC-SQL Refiner is worth +4.63 EX (+6.95 on challenging questions).
- The CHASE-SQL query fixer is worth +3.78 EX.
- Self-Debugging gives +2-3%, and +9% on the hardest level.
- In DIN-SQL, GPT-4 with "generic" self-correction (which assumes the query is buggy) fell to 70.0, below 73.3 with no correction. "Gentle" wording reached 74.2.
- MapleDoctor measured frequent mis-repairs. Detecting problems before repairing fixed 13.8% more queries with 67.4% less latency.

**What developers ask for.**

- Structured, classified error feedback that names the offending identifier and candidate fixes.
- Separate handling for runtime error, timeout and empty result.
- Bounded retries that end with a clear terminal error.
- Prompt or temperature variation on retry.
- Robust extraction of exactly one statement.
- No repair of a query that is clean and returns rows.

**Seen in (6):** Vanna, WrenAI, LangChain, LangGraph, LlamaIndex, CrewAI. Research evidence: CHESS, MAC-SQL, CHASE-SQL, Self-Debugging, DIN-SQL, MapleDoctor, SIRIUS-SQL.

**Evidence**

- [Vanna #279](https://github.com/vanna-ai/vanna/issues/279): allow corrective action when a query errors in the database. Top-12 by reactions.
- [Vanna #754](https://github.com/vanna-ai/vanna/issues/754): a 5-try retry always returns the same output. Retrying without new information does not help.
- [HN: Vanna.ai discussion](https://news.ycombinator.com/item?id=38992601): "most success... when I started feeding errors from the sql provider back to the LLM".
- [LangGraph #6731](https://github.com/langchain-ai/langgraph/issues/6731): a text-to-SQL agent loops until the recursion limit (same tool error, slight variation, repeat).
- [langchain-community #305](https://github.com/langchain-ai/langchain-community/issues/305): the model calls concatenated tool names (`sql_db_query_checkersql_db_query…`) and loops.
- [LangChain #5876](https://github.com/langchain-ai/langchain/issues/5876): the SQL agent throws `OutputParserException` with GPT-4/3.5. Duplicated on [Stack Overflow](https://stackoverflow.com/questions/76421921/using-gpt-4-or-gpt-3-5-with-sql-database-agent-throws-outputparserexception-cou).
- [LlamaIndex #10999](https://github.com/run-llama/llama_index/issues/10999): generated SQL starts with "sql" and causes a syntax error. Earlier parser fixes: [#7278](https://github.com/run-llama/llama_index/issues/7278) and [#7283](https://github.com/run-llama/llama_index/issues/7283).
- [LlamaIndex #22943](https://github.com/run-llama/llama_index/issues/22943): MCP tool failures are reported to the agent as successes (`is_error=False`).
- [CHESS](https://arxiv.org/html/2405.16755): removing revision costs 6.80 EX, and a single pass costs 3.40, the largest ablation in the paper.
- [MAC-SQL](https://arxiv.org/html/2312.11242): the Refiner (execute, feed back error or empty result) is worth +4.63 EX, or +6.95 on challenging questions.
- [DIN-SQL](https://arxiv.org/html/2304.11015): "generic" self-correction lowers GPT-4 to 70.0, against 73.3 with none and 74.2 with "gentle" correction.
- [MapleDoctor](https://arxiv.org/abs/2501.09310): existing repair methods show limited gains and many mis-repairs. Detecting before repairing fixes 13.8% more queries with 67.4% less latency.

---

## 7. Schema semantics and domain knowledge (descriptions, glossary, sample values)

**What breaks.** Bare table and column names give the model "access, not understanding". It picks wrong joins, invents metrics, and writes `WHERE` literals that do not match stored values (for example "Business Trip" against the stored "BUSINESS"). Developers repeatedly ask for column and table comments in the schema context, and they report table-level context being ignored in code. They also ask for value and enum discovery, and for semantic-layer metadata (dbt Semantic Layer, Cube). The semantic-layer request is WrenAI's second most-reacted issue. One project reported that adding column descriptions and foreign-key maps fixed retrieval.

The measured gains are the largest in the literature:

- Per-question external-knowledge "evidence" lifts GPT-4 on BIRD from 30.90 to 46.35 on dev (+15.45 EX) and from 34.88 to 54.89 on test (+20.01).
- Misunderstood database content (values) causes 40.8% of ChatGPT failures.
- Removing value and description retrieval costs 4.76 EX in CHESS. Removing LSH value retrieval costs 2.92 in CHASE-SQL.
- Three distinct values per column add +3.2 (Codex) and +2.6 (ChatGPT) on Spider.
- Foreign keys add +2.1 to +4.6.

**What developers ask for.**

- A per-table, per-column and per-term glossary or hints parameter.
- Column comments pulled from the database.
- Foreign keys always included.
- Up to 3 distinct sample values per text column.
- A literal-value index for low-cardinality columns.
- Ingestion of dbt or semantic-layer metadata.

**Seen in (7):** LangChain, LlamaIndex, Vanna, WrenAI, omi, MotherDuck, MCP Toolbox. Research evidence: BIRD, CHESS, CHASE-SQL, "How to Prompt LLMs for Text-to-SQL", DAIL-SQL.

**Evidence**

- [LangChain #5158](https://github.com/hwchase17/langchain/issues/5158): make the SQL helper use table and column comments.
- [LlamaIndex #6996](https://github.com/run-llama/llama_index/issues/6996): add column comments to SQLTable info. Maintainer: "would love a PR".
- [LlamaIndex #6886](https://github.com/run-llama/llama_index/issues/6886): `context_str` (table-level context) is ignored by SQLTableRetrieverQueryEngine (a TODO in the code).
- [LlamaIndex PR #17479](https://github.com/run-llama/llama_index/issues/17479): add sample rows to the NL-to-SQL context so `WHERE` values match real data.
- [Vanna #161](https://github.com/vanna-ai/vanna/issues/161): scan the database and tables to gather context (enum and value discovery). Requested by users with many tables and enumeration values.
- [WrenAI #1120](https://github.com/canner/wrenai/issues/1120): metrics and semantic-layer integration (dbt Semantic Layer, Cube). Top-2 by reactions in WrenAI.
- [Vanna #162](https://github.com/vanna-ai/vanna/issues/162): integrate with dbt to provide documentation.
- [omi PR #6578](https://github.com/basedhardware/omi/issues/6578): the LLM was "flying blind" on bare column names. Adding column descriptions and FK maps fixed retrieval.
- [MotherDuck: Your data model is the semantic layer](https://motherduck.com/blog/bird-bench-and-data-models/): column comments evaluated on BIRD.
- [BIRD paper](https://arxiv.org/html/2305.03111): external-knowledge evidence gives +15.45 EX on dev and +20.01 on test for GPT-4. Human accuracy rises from 72.37 to 92.96.
- [How to Prompt LLMs for Text-to-SQL](https://arxiv.org/html/2305.11853): CreateTable plus 3 distinct values per column adds +3.2 (Codex) and +2.6 (ChatGPT). Foreign keys add +2.1 to +4.6. Three rows or values worked best.
- [CHASE-SQL](https://arxiv.org/html/2410.01943): removing LSH value retrieval costs 2.92 EX (73.01 to 70.09).

---

## 8. Bounded results with explicit truncation

**What breaks.** Large results overflow the context window. Examples include a 210,004-token prompt, a 3M-row ClickHouse result serialized to 231 MiB, and an 800 KB result that ended a Gemini Live session. Claude Code warns at 10,000 tokens of MCP output and caps it at 25,000 by default.

The more serious failure is silent truncation. DBHub's `max_rows` cap and Metabase's MCP `execute_sql` both cut results at the row limit and reported `completed`, which led to "a confident wrong conclusion". Framework defaults also change answers without saying so: a `LIMIT 10`/`top_k` added to queries, and a `max_string_length` of 300 that truncates values while "Neither caps how many rows a query returns". Truncation has also caused resource leaks. In MCP Alchemy, a truncated query left a cursor open that held a SQLite read lock and blocked all writers.

**What developers ask for.**

- Row and byte caps on every result.
- A result envelope with `row_count`, `truncated`, `limit_applied` and a "narrow your query" hint.
- Fetching `limit+1` rows so the tool knows whether more exist.
- An adjustable `max_rows` clamped to a hard ceiling.
- Structured row artifacts instead of `str(rows)`.
- An export path for full result sets.

**Seen in (13):** LangChain, Metabase, mcp-clickhouse, DBHub, MCP Toolbox, OpenBB, gbrain, Claude Code (client), MCP Alchemy, dbx, Snowflake-managed MCP, MotherDuck MCP, freepeak db-mcp-server.

**Evidence**

- [DBHub #404](https://github.com/bytebase/dbhub/issues/404): the `max_rows` cap fires silently with no truncation signal. Bug label, fixed in #406.
- [Metabase #82850](https://github.com/metabase/metabase/issues/82850): MCP `execute_sql` silently truncates at the row limit and reports `status: completed`.
- [MCP Toolbox #3637](https://github.com/googleapis/mcp-toolbox/issues/3637): declarative per-tool result caps (maxRows / max bytes) with a structured truncation notice. An 800 KB result ended a Gemini Live session.
- [mcp-clickhouse #223](https://github.com/clickhouse/mcp-clickhouse/issues/223): no bound on result size. 3M rows became a 231 MiB payload.
- [LangChain #6836](https://github.com/langchain-ai/langchain/issues/6836): query output exceeds the token limit, and pagination attempts failed. 7 comments, closed "not planned".
- [langchain-community #296](https://github.com/langchain-ai/langchain-community/issues/296): QuerySQLDatabaseTool lacks `response_format="content_and_artifact"`, so no structured rows are available downstream.
- [SO: SQL agent limits answers to 10 rows](https://stackoverflow.com/questions/77204383/my-sql-langchain-agent-seems-to-be-limiting-its-answers-to-10-rows): an unwanted `LIMIT 10`.
- [Render: connecting an AI agent to SQL with LangChain tools](https://render.com/articles/connect-my-ai-agent-to-a-sql-database-with-langchain-tools): `max_string_length` (default 300) truncates values. "Neither caps how many rows a query returns".
- [OpenBB #7315](https://github.com/openbb-finance/openbb/issues/7315): MCP returns very large responses ("prompt is too long: 210004 tokens").
- [Claude Code MCP docs (mirror)](https://github.com/nicepkg/ai-workflow/blob/d167b41af7f013d3d3c35dca87a6cc72817721d2/claude-code-docs/docs/mcp.md): warning at 10,000 tokens, default maximum 25,000, configurable with `MAX_MCP_OUTPUT_TOKENS`.
- [MCP Alchemy](https://github.com/runekaagaard/mcp-alchemy): `EXECUTE_QUERY_MAX_CHARS` (default 4000) and full result sets written to `CLAUDE_LOCAL_FILES_PATH`. Also the cursor-leak fix (#40).
- [dbx PR #9540](https://github.com/t8y2/dbx/issues/9540): replaces a hard-coded 100-row cap with a `max_rows` parameter clamped to 1000.

---

## 9. Painless install and client setup

**What breaks.** Getting the stack running is the top-reacted issue in two of the four apps. In Vanna it is a ChromaDB telemetry error. In DB-GPT it is the complaint "uv not good as pip", followed by "deployment too cumbersome". Other reports include services that will not start, Docker Compose failing on machines with no GPU, unpinned dependencies ("works on my system"), and documentation that links to 404s after a rewrite.

For MCP servers, the most common setup failure is the GUI client not finding `uvx`, `npx` or `uv` (`spawn uvx ENOENT`). Vendors document absolute paths as the fix. Other problems:

- Windows needs a `cmd /c npx` wrapper.
- Each client uses a slightly different JSON shape (`mcpServers`, or VS Code `servers` with `inputs`).
- Connection strings sit in plaintext config.
- Python servers need driver extras (`--with psycopg2-binary`), and the uv cache can raise version errors for up to 600 s after a release.

**What sets the bar.** The leading servers remove this friction with:

- A copy-paste `claude mcp add ... -- npx -y pkg --dsn ...` line.
- MCPB one-click bundles for Claude Desktop, plus one-click Cursor and VS Code links.
- Hosted remote HTTP URLs with OAuth (Supabase, Neon), which avoid local install entirely.
- Streamable HTTP (with an auth token required) for Docker and shared deployments.

**Seen in (14):**

- Apps: Vanna, WrenAI, DB-GPT, sqlcoder.
- MCP servers and tooling: MCP Python SDK, dbt MCP, mcp-neo4j, Postgres MCP Pro, Zscaler MCP, MCP Toolbox, MCP Alchemy, DBHub, MotherDuck MCP, McpMux.

**Evidence**

- [Vanna #917](https://github.com/vanna-ai/vanna/issues/917): ChromaDB telemetry error. **#1 by reactions in Vanna**, 10 comments.
- [DB-GPT #2561](https://github.com/eosphoros-ai/DB-GPT/issues/2561): "wish uv to pip, uv not good as pip". **#1 by reactions in DB-GPT**.
- [DB-GPT #158](https://github.com/eosphoros-ai/DB-GPT/issues/158): deployment is too cumbersome; asks for one-click startup. Top-3 by reactions.
- [WrenAI #1785](https://github.com/canner/wrenai/issues/1785): wren-ai-service cannot start. Top-25 by reactions, 9 comments.
- [Vanna #236](https://github.com/vanna-ai/vanna/issues/236): unpinned dependencies cause "works on my system" failures in production.
- [MCP python-sdk #478](https://github.com/modelcontextprotocol/python-sdk/issues/478): `mcp install` fails with "spawn uv ENOENT" in Claude Desktop. Affects every Python MCP server.
- [dbt MCP troubleshooting](https://docs.getdbt.com/docs/dbt-ai/mcp-troubleshooting.md): documents `spawn uvx ENOENT` and the fix of using a full path (`/opt/homebrew/bin/uvx`).
- [mcp-neo4j #41](https://github.com/neo4j-contrib/mcp-neo4j/issues/41): Claude Desktop "spawn uvx ENOENT". A duplicate across database servers.
- [Claude Code MCP guide (mcpbundles)](https://www.mcpbundles.com/blog/claude-code-mcp-tools): "Connection closed" on Windows; wrap with `cmd /c npx`.
- [McpMux guide](https://github.com/mcpmux/mcp-mux/blob/2e6a5b925eb4b2df1ffb292b1216101aef872f0c/docs/guide/index.mdx): duplicate configs per client and plaintext credentials in config files.
- [DBHub 1.0 release post](https://github.com/bytebase/dbhub/blob/2dbf01af53e34784eafc00d777394ec844b98eca/docs/blog/dbhub-1-0-0.mdx): MCPB bundle and a Claude Code plugin. The Claude Code docs use DBHub as their Postgres example. 73 npm releases and 3,200+ stars at 1.0.
- [Neon MCP](https://github.com/neondatabase/mcp-server-neon): hosted remote server with OAuth and `?roleName=` scoping, offering a path with nothing to install.

---

## 10. Framework adapters and MCP exposure

**What developers ask for.** Users want text-to-SQL available as a tool inside agent frameworks and MCP clients. MCP support is Vanna's second most-reacted issue (17 comments). Agentic, LangChain and LlamaIndex integration is top-4 in Vanna, and LangChain integration is top-12 in sqlcoder. WrenAI and DB-GPT have since shipped MCP, and DB-GPT works as both MCP client and server. Third parties filled the gaps with a community Vanna MCP server and a LlamaPack wrapper.

`langchain-community` has been sunset. The code that holds `SQLDatabaseToolkit` and `create_sql_agent` no longer has an owner, and open SQL bugs were bulk-closed without fixes. LangChain's own reasoning is that developers now write tools in application code or consume them over MCP. LangChain's four `sql_db_*` tools are the de facto shape that other projects copy. One competing project already ships LangChain, LlamaIndex and ADK adapters that call the same functions as its MCP server, including a `validate_sql` tool.

Each framework has its own tool and error conventions (see the interface table under Ecosystem notes). Getting error semantics wrong is a known pitfall. LlamaIndex stringifies unknown returns with `is_error=False`. The Claude Agent SDK expects `is_error` to be returned rather than raised. The OpenAI Agents SDK offers tool input guardrails, which suit validation that runs before execution.

**Seen in (9):** Vanna, WrenAI, DB-GPT, sqlcoder, LangChain, LlamaIndex, pretensor, Agno, OpenAI Agents SDK.

**Evidence**

- [Vanna #721](https://github.com/vanna-ai/vanna/issues/721): support for MCP. **#2 by reactions in Vanna**, 17 comments.
- [Vanna #488](https://github.com/vanna-ai/vanna/issues/488): add agentic applications (ReAct, LangChain/LlamaIndex integration). Top-4 by reactions.
- [sqlcoder #88](https://github.com/defog-ai/sqlcoder/issues/88): LangChain integration. Top-12 by reactions, 6 comments.
- [WrenAI #2337](https://github.com/canner/wrenai/issues/2337): MCP server integration. Requested and shipped (Jul 2026).
- [DB-GPT MCP docs](https://github.com/eosphoros-ai/db-gpt/blob/8733ce9071dbbfb09d3612c0d60a3d9bb3c66e20/docs/docs/getting-started/tools/mcp.md): DB-GPT as both MCP client and server.
- [lilgreml1n/vanna-mcp](https://github.com/lilgreml1n/vanna-mcp): community Vanna MCP server, built by a third party to fill the gap.
- [llama-index-packs-vanna](https://pypi.org/project/llama-index-packs-vanna/): third-party LlamaPack wrapper.
- [langchain-community #674](https://github.com/langchain-ai/langchain-community/issues/674): sunsetting `langchain-community`, citing coding agents and MCP as the reason. Referenced by every closed SQL issue.
- [pretensor agent framework adapters](https://github.com/pretensor-ai/pretensor/blob/f9ea08a14a7312683dabb8c45e3314ea9aa2e976/docs/agent-framework-adapters.md): native LangChain, LlamaIndex and ADK adapters, including `validate_sql`, that share functions with its MCP server.
- [vinagent banking agent guide](https://github.com/datascienceworld-kan/vinagent/blob/f13dd35fe2f8a3d0f4c98755d223fe1bdce34795/docs/docs/guides/banking_agent.md): an example of the cloned `sql_db_*` tool set.
- [OpenAI Agents SDK guardrails](https://github.com/openai/openai-agents-python/blob/64cd5dfc5a1579870dcbed0ea42b27aa84033fee/docs/guardrails.md): tool input and output guardrails (allow, reject_content, raise).
- [Claude Agent SDK (Python)](https://code.claude.com/docs/en/agent-sdk/python): `@tool` with async handlers, `create_sdk_mcp_server`, and an `is_error` result field.

---

## 11. Connectivity and connector breadth (multi-DB, schemas, views, SSL/SSH)

**What breaks.** Connector requests dominate WrenAI's most-reacted list. MSSQL is #1, followed by Oracle, MongoDB, Trino, MySQL, Databricks and Doris. Vanna users ask for multiple connections, custom Postgres schemas, Snowflake key-pair authentication and NoSQL, and DB-GPT users ask repeatedly for Oracle.

The generic SQLAlchemy wrappers in the frameworks break on:

- non-public Postgres schemas (`SET search_path`);
- BigQuery (`SET @@dataset_id`);
- multiple schemas;
- views and materialized views;
- MSSQL cursors (integration tests only cover SQLite).

Many of these were closed unfixed by the langchain-community sunset.

On the MCP side, users with dev, staging and prod databases do not want one server entry per database. Each extra entry also counts against tool caps. Multi-database configuration became a headline feature (DBHub TOML, pgEdge named connections). Real deployments need TLS with self-signed or RDS CAs, `verify-ca`/`verify-full`, client certificates, SSH tunnels through bastions, URL-encoding-safe DSNs and connection pooling with pre-ping. DBHub alone has four separate SSL issues, and one MySQL server maintainer declined SSH tunnel support.

**Seen in (12):**

- Apps: WrenAI, Vanna, DB-GPT.
- Frameworks: LangChain, LlamaIndex.
- MCP servers: DBHub, mysql_mcp_server, mcp-server-mysql (benborla), Supabase, pgEdge MCP, Postgres MCP Pro, MotherDuck MCP.

**Evidence**

- [WrenAI #173](https://github.com/Canner/WrenAI/issues/173): MS SQL Server connector. **#1 by reactions in WrenAI**.
- [WrenAI #236](https://github.com/Canner/WrenAI/issues/236): OracleDB support. Top-5 by reactions.
- [Vanna #285](https://github.com/vanna-ai/vanna/issues/285): support for multiple database connections. Top-20 by reactions, 7 comments.
- [langchain-community #285](https://github.com/langchain-ai/langchain-community/issues/285): SQLDatabase with a Postgres schema fails (`SET search_path TO %s`). Closed by the sunset.
- [langchain-community #201](https://github.com/langchain-ai/langchain-community/issues/201): BigQuery `SET @@dataset_id=?` fails.
- [LangChain #10811](https://github.com/langchain-ai/langchain/issues/10811): SQLAgent cannot use multiple Postgres schemas. Related: [LlamaIndex #16644](https://github.com/run-llama/llama_index/issues/16644).
- [LangChain #4349](https://github.com/langchain-ai/langchain/issues/4349): materialized views are not accessible under SQLAlchemy 2.0.
- [langchain-community #107](https://github.com/langchain-ai/langchain-community/issues/107): `_execute(fetch="cursor")` returns a closed cursor on MSSQL. Integration tests only cover SQLite.
- [DBHub #7](https://github.com/bytebase/dbhub/issues/7): "Does it support multiple database connections?" The workaround was two servers plus a system prompt. Now a core feature.
- [DBHub #293](https://github.com/bytebase/dbhub/issues/293): support `verify-ca`/`verify-full` and `sslrootcert`. One of four SSL issues, with [#33](https://github.com/bytebase/dbhub/issues/33), [#57](https://github.com/bytebase/dbhub/issues/57) and [PR #441](https://github.com/bytebase/dbhub/issues/441).
- [mysql_mcp_server #10](https://github.com/designcomputer/mysql_mcp_server/issues/10): add SSH tunnel support. The maintainer declined ("use PuTTY").
- [DBHub PR #17](https://github.com/bytebase/dbhub/issues/17): passwords containing `^ = ;` fail authentication (URL encoding).

---

## 12. Cost, latency and timeout guards

**What developers ask for.** There are two groups. Users of multi-stage pipelines object to the number of LLM calls per question. They ask to reduce requests, to turn off extras such as question recommendation, and to skip column pruning for small schemas. They also report slow heavy imports: 1m49s per query from torch/transformers. Warehouse users want to know what a query costs before it runs. They ask for dry-run estimates, `maximum_bytes_billed`, and limits the agent can see so that it can narrow the query itself.

Operational reports add per-query timeouts and cancellation that does not wedge the server. In one report, a single cancelled query poisoned the connection pool and every database call hung for 50 minutes while `/health` returned 200. EXPLAIN is now a first-class tool in leading Postgres MCP servers. Rated medium because most individual items are single reports, although they span 12 projects.

**Seen in (12):** DB-GPT, WrenAI, sqlcoder, Vanna, Lightdash, Builder agent-native, MCP Toolbox, ergut BigQuery MCP, gbrain, Postgres MCP Pro, DBHub, MotherDuck MCP.

**Evidence**

- [DB-GPT #83](https://github.com/eosphoros-ai/DB-GPT/issues/83): reduce the number of LLM requests to cut time cost. Top-2 by reactions in DB-GPT.
- [WrenAI #1202](https://github.com/Canner/WrenAI/issues/1202): allow switching off question recommendation (extra LLM calls). Top-10 by reactions.
- [WrenAI #1848](https://github.com/canner/wrenai/issues/1848): avoid unnecessary LLM calls in column pruning for small schemas.
- [WrenAI #2611](https://github.com/canner/wrenai/issues/2611): `wren memory` takes 1m49s per query because of torch/transformers imports.
- [Vanna PR #563](https://github.com/vanna-ai/vanna/issues/563): `max_tokens` is used for both prompt size and output length. +12 reactions on a comment.
- [Lightdash connect-project docs](https://github.com/lightdash/mintlify-docs/blob/9f3dd17e4ff1c2100e85567ba434c5881440d128/integrations/connect-project.mdx): separate "AI agent maximum bytes billed" and a dry-run "AI agent query limit". The agent sees the estimate and can narrow the query.
- [Builder agent-native PR #7710](https://github.com/builderio/agent-native/issues/7710): "Nothing… says what a query costs before it runs". Adds an `estimate-query-cost` dry-run action.
- [MCP Toolbox bigquery-execute-sql](https://github.com/googleapis/mcp-toolbox/blob/0e9e80bdae3c7e620d5214e3bb86f8bbbe90acf2/docs/en/integrations/bigquery/tools/bigquery-execute-sql.md): `dry_run` parameter, `writeMode` and `allowedDatasets`.
- [ergut BigQuery MCP](https://glama.ai/mcp/servers/ergut/mcp-bigquery-server): dry-run planner and `--maximum-bytes-billed`.
- [gbrain #6383](https://github.com/garrytan/gbrain/issues/6383): one client-cancelled query poisons the pool. Every database MCP call hangs for 50 minutes while `/health` returns 200.
- [Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp): `explain_query`, `analyze_query_indexes` and `get_top_queries` as first-class tools.

---

## 13. Broad LLM provider support

**What developers ask for.** Users want to swap providers freely: local and self-hosted (Ollama, vLLM, any OpenAI-compatible endpoint) as well as cloud (Azure, Bedrock, Gemini). Ollama support was requested in Vanna before and again after the 2.0 rewrite. A large WrenAI thread reports that self-hosted and Ollama models do not work, and configuring LLMs from the web UI is top-10 by reactions. Provider SDK changes break integrations, for example an `AsyncClient` `proxies` kwarg error with DeepSeek, Azure OpenAI breaking after an API change, and Gemini 3 `thought_signature` handling for function calls. Users also want specialized SQL models such as sqlcoder to be pluggable. This need was found only in the apps pass, but it appears in three projects with repeated requests, so it is rated strong.

**Seen in (3):** Vanna, WrenAI, DB-GPT.

**Evidence**

- [Vanna #167](https://github.com/vanna-ai/vanna/issues/167): Ollama support (fully local with Chroma and Ollama). Re-asked for 2.0 in [#984](https://github.com/vanna-ai/vanna/issues/984).
- [Vanna #1104](https://github.com/vanna-ai/vanna/issues/1104): does Vanna 2.0 support vLLM or OpenAI-compatible local models?
- [Vanna #174](https://github.com/vanna-ai/vanna/issues/174): cannot use AzureOpenAI (duplicate of #171). Broke after a provider API change.
- [Vanna #456](https://github.com/vanna-ai/vanna/issues/456): connection with AWS Bedrock.
- [Vanna #1073](https://github.com/vanna-ai/vanna/issues/1073): Gemini 3 support, specifically `thought_signature` handling for function calling.
- [WrenAI #1777](https://github.com/canner/wrenai/issues/1777): self-hosted and Ollama models are not working. Large thread.
- [WrenAI #1421](https://github.com/canner/wrenai/issues/1421): configure LLMs from the web interface. Top-10 by reactions.
- [DB-GPT #2352](https://github.com/eosphoros-ai/db-gpt/issues/2352): DeepSeek error, `AsyncClient` got an unexpected kwarg `proxies` (SDK version break).
- [DB-GPT #2198](https://github.com/eosphoros-ai/db-gpt/issues/2198): `proxyllm_backend` in `.env` is not honored with ollama_proxyllm.
- [Vanna #303](https://github.com/vanna-ai/vanna/issues/303): sqlcoder LLM support. Related: [DB-GPT #547](https://github.com/eosphoros-ai/db-gpt/issues/547), with many "+1" comments.

---

## 14. Eval and regression testing with a trustworthy comparator

**What developers ask for.** Teams need golden question-to-SQL sets and execution-accuracy scoring so they can catch regressions when prompts, models or schemas change. Most of the demand shows up as vendor investment and stated gaps rather than user issues. WrenAI built an eval epic and still lists regression detection as "in development". DB-GPT ships a benchmark module. Defog maintains sql-eval. Elastic's Kibana replaced a LangSmith-hosted workflow with its own suite. Framework trackers carry little direct demand, and teams build their own scorers in Ragas, promptfoo, DeepEval or custom code.

**What breaks in the tooling.** The defects here are well measured:

- BIRD EX compares sets, which ignores duplicates. This overstates accuracy by 3.39-6.79 points.
- The BIRD mini_dev evaluator pairs predictions with gold queries by position. A key-sorted file scored 0.0013 instead of 0.9987, with no error.
- Float sums on Postgres are non-deterministic, so exact-float comparison flips between 1 and 0 across runs.
- Gold annotations are wrong at high rates: 20.7% in one BIRD domain, 52.8% on BIRD Mini-Dev and 62.8% on Spider 2.0-Snow. Correcting them reorders leaderboards.
- A deployed gpt-4o-mini judge agreed with humans at kappa 0.04.
- Parser-based assertions such as promptfoo `is-sql` reject valid SQL.

**What developers ask for (comparator).**

- Multiset comparison by default.
- Order checked only when the gold query has `ORDER BY`.
- A float tolerance.
- Column-subset matching with alias mapping.
- Predictions keyed by id rather than position.
- Set-EX reported alongside Multiset-EX.
- The same lint applied to gold queries as to predictions.
- A CI regression gate.

**Seen in (12):** WrenAI, DB-GPT, Vanna, Defog sql-eval, IDinsight, Kibana, Ragas, promptfoo, DeepEval, Braintrust, BIRD mini_dev, Spider test-suite-sql-eval.

**Evidence**

- [WrenAI #355](https://github.com/canner/wrenai/issues/355): [EPIC] first version of the Wren AI Service evaluation framework.
- [WrenAI architecture doc](https://github.com/canner/wrenai/blob/5a287c5befd6f52db71d12593d0848f2dd0c0299/docs/core/reference/architecture.md): "Eval: detecting regressions when schemas, definitions, or prompts change... in development".
- [Kibana PR #268787](https://github.com/elastic/kibana/issues/268787): replaces the LangSmith-hosted ES|QL regression workflow with its own suite (LLM equivalence, AST validity, live execution, result-row Jaccard), run weekly across models.
- [Defog sql-eval](https://github.com/defog-ai/sql-eval): result-based comparison rather than string comparison. Widely referenced.
- [Ragas: evaluate a text-to-SQL agent](https://docs.ragas.io/en/stable/howtos/applications/text2sql/): execution accuracy via datacompy.
- [ModularSQL "Multiplicity Blind Spot"](https://arxiv.org/html/2609.29573v1): Set-EX overstates Multiset-EX by 5.81-6.79 points (DeepEye-SQL), 5.22 (DAIL-SQL+GPT-4) and 3.39 (GPT-3.5 baseline).
- [bird-bench/mini_dev #41](https://github.com/bird-bench/mini_dev/issues/41): positional pairing. A `sort_keys=True` dump scores 0.0013 instead of 0.9987, silently. Reproduced.
- [bird-bench/mini_dev #48](https://github.com/bird-bench/mini_dev/issues/48): Postgres parallel aggregation makes float sums non-deterministic, so exact comparison scores 1 or 0 on different runs. Reproduced.
- [Examination of BIRD-Bench](https://arxiv.org/html/2402.12243v4): 20.7% of BIRD dev financial-domain gold items are incorrect. Correcting gold reorders methods.
- [Pervasive Annotation Errors Break Text-to-SQL Benchmarks (VLDB 2026)](https://www.vldb.org/pvldb/vol19/p931-jin.pdf): 52.8% error rate on BIRD Mini-Dev and 62.8% on Spider 2.0-Snow. Ranks move by up to ±9.
- [promptfoo #9784](https://github.com/promptfoo/promptfoo/issues/9784): parser-heuristic false rejects (`SELECT DISTINCT name` failed).
- [Braintrust: evaluating SQL queries that return wrong answers](https://www.braintrust.dev/articles/how-to-evaluate-sql-queries-that-return-wrong-answers): explicit comparator rules (multisets, ordering only for ranking questions, float tolerance, NULL/0/empty kept distinct, pinned snapshot) and a CI regression gate.

---

## 15. Transparency, approval and audit (show SQL, SQL-only mode, HITL)

**What developers ask for.** Users want to see the exact SQL that ran, for display, debugging and audit. Retrieving it from `create_sql_agent` is a popular Stack Overflow question. They also want a mode that generates SQL without executing it (LlamaIndex merged `sql_only`), a hook to intercept and review SQL before execution, and approval gates. Frameworks have added these separately: LangChain added a HITL interrupt before `sql_db_query`, and the OpenAI Agents SDK added tool input guardrails and pre-approval guardrails. The Gemini cookbook notes that its existing SQL notebooks have "no guard or approval step". Vanna 2.0 repositioned around audit logs, and MCP Toolbox users ask for audit logs alongside masking. Rated medium because most items are single, low-engagement issues.

**Seen in (6):** LangChain, LlamaIndex, OpenAI Agents SDK, Gemini cookbook, Vanna, MCP Toolbox.

**Evidence**

- [SO: retrieve the generated SQL from `create_sql_agent`](https://stackoverflow.com/questions/77953302/how-to-retrieve-the-generated-sql-query-from-create-sql-agent-of-langchain): popular Q&A.
- [LlamaIndex PR #9422](https://github.com/run-llama/llama_index/issues/9422): `sql_only` hook that returns the final SQL without running it. Merged.
- [LlamaIndex #14168](https://github.com/run-llama/llama_index/issues/14168): SQLTableRetrieverQueryEngine response metadata is wrongly formatted.
- [LangChain SQL agent tutorial (Python)](https://docs.langchain.com/oss/python/langchain/sql-agent): HITL interrupt before `sql_db_query`; errors returned to the model for retry.
- [OpenAI Agents SDK guardrails](https://github.com/openai/openai-agents-python/blob/64cd5dfc5a1579870dcbed0ea42b27aa84033fee/docs/guardrails.md): tool input/output guardrails. Pre-approval guardrails in [#3487](https://github.com/openai/openai-agents-python/issues/3487).
- [OpenAI Agents #4637](https://github.com/openai/openai-agents-python/issues/4637): allow `Agent.as_tool()` to use tool input and output guardrails.
- [Gemini cookbook #1393](https://github.com/google-gemini/cookbook/issues/1393): proposed "Safe SQL agent with LangGraph". The existing notebooks have "no guard or approval step".
- [Vanna 2.0 README](https://github.com/vanna-ai/vanna): "Row-level security, audit logs, rate limiting" and "User-Aware at Every Layer".

---

## Themes not ranked (lower signal)

- **Async and streaming.** There is low volume of demand: [LangChain #11732](https://github.com/langchain-ai/langchain/issues/11732) (async SQLDatabaseChain), [LlamaIndex PR #13599](https://github.com/run-llama/llama_index/issues/13599) (streaming), and [Temporal sdk-python #1718](https://github.com/temporalio/sdk-python/issues/1718) (database I/O in `@function_tool` runs inside the workflow by accident).
- **Natural-language result summaries.** Vendors have shipped these: [Vanna #181](https://github.com/vanna-ai/vanna/issues/181) and [WrenAI `/v2/generate_summary`](https://wrenai.readme.io/reference/post_generate-summary-1.md), which samples 500 rows. This feature interacts with the "no row data to the LLM" control in need 5.
- **API stability and learned-memory correctness.** There was backlash against the Vanna 2.0 rewrite in [Vanna #1052](https://github.com/vanna-ai/vanna/issues/1052) (top-6 by reactions). The [migration guide](https://github.com/vanna-ai/vanna/blob/365d0617c1a4567ffee1b19b40c27feb4206bfcf/MIGRATION_GUIDE.md) says the legacy adapter is "Missing some advanced features". Users asked to clear historical Q&A in [Vanna #919](https://github.com/vanna-ai/vanna/issues/919). Taken together, these favor a small, stable core API and validation before any example is saved.

---

## Quality levers with measured impact

Gains are as reported by each source, on its own benchmark and model. EX is execution accuracy in percentage points. The "fits" column judges whether the technique can be built in a small Python library without trained models or heavy infrastructure.

| Technique | Measured gain | Source | Fits a small Python library? |
|---|---|---|---|
| Domain knowledge / evidence hints in the prompt | GPT-4 BIRD dev 30.90 to 46.35 (+15.45 EX); test 34.88 to 54.89 (+20.01); ChatGPT +13.17, Claude-2 +14.41 (dev) | [BIRD](https://arxiv.org/html/2305.03111) | Yes |
| Execution-feedback revision loop | Removing it: -6.80 EX; single pass instead of several: -3.40 | [CHESS](https://arxiv.org/html/2405.16755) | Yes |
| Refiner (execute, feed back error or empty result) | +4.63 EX overall, +6.95 on challenging | [MAC-SQL](https://arxiv.org/html/2312.11242) | Yes |
| Query fixer on execution errors | +3.78 EX (BIRD dev) | [CHASE-SQL](https://arxiv.org/html/2410.01943) | Yes |
| Self-debugging with code explanation | +2-3% on Spider; +9% on hardest level; matches baselines sampling 10x more | [Self-Debugging](https://arxiv.org/abs/2304.05128) | Yes |
| Repair prompt wording ("gentle" vs "generic") | GPT-4: gentle 74.2, none 73.3, generic 70.0 | [DIN-SQL](https://arxiv.org/html/2304.11015) | Yes |
| Detect-then-repair (gate repair on a detector) | 13.8% more queries repaired, negligible mis-repairs, -67.4% latency | [MapleDoctor](https://arxiv.org/abs/2501.09310) | Yes |
| Outcome-specific repair (error vs timeout vs empty) | 75.88% BIRD dev (no isolated delta reported) | [SIRIUS-SQL](https://arxiv.org/abs/2606.01246) | Yes |
| Value and column-description retrieval | Removing both: -4.76 EX | [CHESS](https://arxiv.org/html/2405.16755) | Partly (needs a value index) |
| LSH literal-value retrieval | Removing it: -2.92 EX (73.01 to 70.09) | [CHASE-SQL](https://arxiv.org/html/2410.01943) | Partly (needs a value index) |
| 3 distinct values per column in the prompt | +3.2 (Codex), +2.6 (ChatGPT); 3 sample rows +1.5 / +1.1 | [How to Prompt](https://arxiv.org/html/2305.11853) | Yes |
| Foreign keys in the prompt | +2.1 to +4.6; +0.6 to +2.9 across representations | [How to Prompt](https://arxiv.org/html/2305.11853), [DAIL-SQL](https://arxiv.org/html/2308.15363) | Yes |
| "With no explanation" rule (SQL only) | about +3% EX (+6% exact match) | [DAIL-SQL](https://arxiv.org/html/2308.15363) | Yes |
| Skip schema linking when the schema fits | Full schema 67.35 vs 62.58 / 55.78 with linking; linker recall 77.4-90.4% | [Death of Schema Linking?](https://arxiv.org/html/2408.07702) | Yes (size threshold) |
| Schema pruning on larger schemas | MAC-SQL selector +2.11 (+5.14 challenging); DIN-SQL schema linking +4.0; CHESS selector about +2% at 5x fewer tokens | [MAC-SQL](https://arxiv.org/html/2312.11242), [DIN-SQL](https://arxiv.org/html/2304.11015), [CHESS](https://arxiv.org/html/2405.16755) | Yes |
| Bidirectional schema linking | 94% strict recall while removing 83% of columns; 67.2% BIRD, 87.9% Spider | [RSL-SQL](https://arxiv.org/abs/2411.00073) | Partly |
| Table-first + column-first retrieval | Narrows the full-vs-oracle-schema gap by 50% | [Rethinking Schema Linking](https://arxiv.org/abs/2510.14296) | Partly |
| Pruning with FD reranking and connected join paths | Near-perfect recall, sub-second, 23,000+ columns | [Schema filtering w/ FD graph](https://arxiv.org/abs/2512.16083) | Partly |
| Query-agnostic schema compression | 2.6M to 34.7K tokens; linking recall 0% to 56.5%; +1.8 to +1.9 EX | [DBCC](https://arxiv.org/abs/2606.28601) | Partly |
| Agentic iterative schema exploration | Linking recall 97.4% BIRD dev, 91.2% Spider 2.0-Lite; 68.7% EX; robust past 3,000 columns | [AutoLink](https://arxiv.org/abs/2511.17190) | No (agent loop over large schemas) |
| Schema truncation for open models | Code Llama-7B 14.54 to 48.24 (BIRD dev) | [Open-SQL](https://arxiv.org/abs/2405.06674) | Partly |
| Few-shot by masked-question / SQL-skeleton similarity | DAIL-SQL 86.6% Spider; beats random and raw-question similarity | [DAIL-SQL](https://arxiv.org/html/2308.15363) | Partly (needs verified pair store + embeddings) |
| Structure-aware example retrieval | +2.9 EX over generic embeddings; +2.5 EX over prior SOTA | [MARLO](https://arxiv.org/abs/2410.14049), [Nan et al.](https://arxiv.org/abs/2305.12586) | Partly |
| Self-consistency | +0.4 (86.2 to 86.6); replacing a trained selector with it costs 4.17 | [DAIL-SQL](https://arxiv.org/html/2308.15363), [CHASE-SQL](https://arxiv.org/html/2410.01943) | Partly (cost scales with n) |
| Runtime anomaly guardrail (duplication ratio >= 0.80, empty, error) | Multiset-EX 65.86 to 67.75 (+1.89); $0.0076 total LLM cost; 120 ms amortized | [ModularSQL](https://arxiv.org/html/2609.29573v1) | Yes |
| Outcome reward model selection | Up to +4.33 (BIRD), +2.10 (Spider) over best-of-N / majority vote | [GradeSQL](https://arxiv.org/abs/2606.30851) | No (trained model) |
| Judge ensemble for abstention | Two-provider judge AUROC 0.82 (ECE 0.03) vs 0.61-0.68 for self-consistency / executability / logprob | [Selective prediction](https://arxiv.org/abs/2607.06799) | Partly |
| Multiset comparator in eval | Set-EX overstates by 3.39-6.79 points | [ModularSQL](https://arxiv.org/html/2609.29573v1) | Yes |
| Enhanced tree matching in eval | FP/FN 23.0% / 28.9% reduced to 0.3% / 2.7% | [ETM](https://arxiv.org/abs/2407.07313) | Partly |
| LLM-judge eval (FLEX) | Agreement with experts kappa 62 (EX) to 87.04 | [FLEX](https://arxiv.org/abs/2409.19014) | Partly (judge must be validated) |
| EXPLAIN gating + NULL guards in eval | Flagged 89-100% of false-positive EX passes | [DRL middleware](https://arxiv.org/abs/2608.26172) | Yes |
| Deterministic join-fanout lint on gold | 30/30 Spider and 14/15 BIRD flags confirmed, 0 spurious | [sqlsure audit](https://sqlsure.ai/blog/bird-benchmark-wrong-gold/) | Yes |

Related facts from the same research pass:

- Spider 2.0 SOTA systems combine schema compression, cross-dialect self-refinement, majority voting and execution-guided column exploration. ReFoRCE reaches 35.83 on Snow and 36.56 on Lite ([ReFoRCE](https://arxiv.org/abs/2502.00675)).
- A sample-K=8, execute-all, vote-by-result recipe gets a 7B model to 58.5% on BIRD dev ([practitioner card](https://friendli.ai/models/jk200201/qwen2.5-coder-7b-bird-cot)). This is a single source and is rated weak.

---

## Ecosystem notes

- **langchain-community is sunset, and `SQLDatabaseToolkit` has no owner.** The package that contains `SQLDatabase`, `SQLDatabaseToolkit`, `create_sql_agent`, `QuerySQLDatabaseTool` and `QuerySQLCheckerTool` has been sunset. Open issues were bulk-closed without fixes, many of them SQL bugs. LangChain cites coding agents and MCP as the reason. Sources: [langchain-community #674](https://github.com/langchain-ai/langchain-community/issues/674), [README](https://github.com/langchain-ai/langchain-community), [PyPI](https://pypi.org/project/langchain-community/), [SO: how to adjust](https://stackoverflow.com/questions/79991625/langchain-community-is-deprecated-or-sunset-how-to-adjust-to-this). The official LangChain JS SQL agent tutorial now writes its own `sanitizeSqlQuery` and "READ-ONLY" tool ([snippet](https://github.com/langchain-ai/docs/blob/332b12ee879b1d04ec47dec765627c009eb84b7a/src/snippets/code-samples/sql-agent-execute-sql-js.mdx)).
- **Vanna was archived on 2026-03-29.** The banner on the issues page says Mar 29, 2026. Vanna 2.0 was a full rewrite, and many 0.x issues were mass-closed with "likely fixed in v2". The 2.0 line has its own security reports: [Vanna #1121](https://github.com/vanna-ai/vanna/issues/1121) (CVE-2026-4229) and [VulDB: missing authentication on all API endpoints](https://vuldb.com/submit/780727).
- **The archived MCP reference database servers are still heavily used.** `@modelcontextprotocol/server-postgres` is archived and unpatched but still had 95,430 downloads/week. The four archived reference servers together had about 214k installs/week ([servers #4785](https://github.com/modelcontextprotocol/servers/issues/4785), [servers-archived](https://github.com/modelcontextprotocol/servers-archived)).
- **MCP client limits.**
  - Cursor sends only the first 40 tools ([Supabase #52](https://github.com/supabase/mcp/issues/52), [Cursor forum](https://forum.cursor.com/t/tools-limited-to-40-total/67976)).
  - Claude Code warns at 10,000 tokens of MCP output and defaults to a 25,000-token maximum, configurable with `MAX_MCP_OUTPUT_TOKENS` ([docs mirror](https://github.com/nicepkg/ai-workflow/blob/d167b41af7f013d3d3c35dca87a6cc72817721d2/claude-code-docs/docs/mcp.md)).
  - GitHub Copilot CLI hit a compaction loop when MCP tool schemas reached about 573K tokens ([copilot-cli #3542](https://github.com/github/copilot-cli/issues/3542)).
- **sqlglot is not a validator by design.** The official FAQ says "SQLGlot is a transpiler, not a validator". The parser is intentionally lenient, and omitting `read=` parses with a superset dialect ([sqlglot.com](https://sqlglot.com/)). Parser error richness is controlled by `error_level`, `error_message_context` and `max_errors` ([parser docs](https://sqlglot.com/sqlglot/parsers/athena.html)).

### The install bar: what popular MCP database servers expose

Stars and download counts were scraped on 2026-10-20.

| Server (stars) | DBs | Default tools | Install one-liner (as documented) | Notable safety knobs |
|---|---|---|---|---|
| [archived `@modelcontextprotocol/server-postgres`](https://github.com/modelcontextprotocol/servers-archived) (95k npm dl/week) | Postgres | `query` + table-schema resources | `npx -y @modelcontextprotocol/server-postgres postgresql://localhost/mydb` | `BEGIN READ ONLY` only, bypassable; unmaintained |
| [MCP Toolbox for Databases](https://github.com/googleapis/mcp-toolbox) (16.7k) | Many, incl. Postgres, MySQL, MariaDB, SQL Server, Oracle, BigQuery, Snowflake, ClickHouse, Trino | prebuilt postgres: `execute_sql`, `list_tables`, + stats tools; custom YAML tools | `npx -y @toolbox-sdk/server --prebuilt=postgres --stdio` (+ `POSTGRES_*` env) | per-source readOnly/writeMode (BigQuery), allowedDatasets, custom parameterized tools |
| [DBHub](https://github.com/bytebase/dbhub) (3.7k) | Postgres, MySQL, MariaDB, SQL Server, Oracle, SQLite | `execute_sql`, `search_objects` (opt: `explain_sql`, `health_check`, custom) | `claude mcp add --transport stdio db -- npx -y @bytebase/dbhub --dsn "postgresql://..."` | readonly + engine read-only, max_rows, TOML multi-db, SSH ProxyJump, SSL modes, MCPB bundle |
| [Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp) (3.4k) | Postgres | `list_schemas`, `list_objects`, `get_object_details`, `execute_sql`, `explain_query`, `get_top_queries`, `analyze_workload_indexes`, `analyze_query_indexes`, `analyze_db_health` | `uvx postgres-mcp --access-mode=restricted` (env `DATABASE_URI`) or docker | restricted/unrestricted (default unrestricted), AST allowlist (bypassed, #178) |
| [Supabase MCP](https://github.com/supabase/mcp) (3.0k) | Supabase Postgres | `list_tables`, `execute_sql`, `apply_migration`, branching, project tools | `{"type":"http","url":"https://mcp.supabase.com/mcp"}` (OAuth) | `read_only=true`, `project_ref`, `features=` groups |
| [mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse) (889) | ClickHouse, chDB | `run_query`, `list_databases`, `list_tables`, `run_chdb_select_query` | `uv run --with mcp-clickhouse --python 3.12 mcp-clickhouse` (+ `CLICKHOUSE_*` env) | `CLICKHOUSE_ALLOW_WRITE_ACCESS=false` (bypassed, #131); HTTP auth token required |
| [mcp-server-motherduck](https://github.com/motherduckdb/mcp-server-motherduck) (532) | DuckDB, MotherDuck | `execute_query`, `list_databases`, `list_tables`, `list_columns`, `switch_database_connection` | `uvx mcp-server-motherduck --db-path :memory:` (read-only default since v1.0; add `--read-write` to allow writes) | 1024 rows / 50k chars caps, MCPB bundle |
| [MCP Alchemy](https://github.com/runekaagaard/mcp-alchemy) (421) | Any SQLAlchemy DB | `all_table_names`, `filter_table_names`, `schema_definitions`, `execute_query` | `uvx --from mcp-alchemy --with psycopg2-binary mcp-alchemy` (env `DB_URL`) | `EXECUTE_QUERY_MAX_CHARS`; no read-only mode (AUTOCOMMIT) |
| [Neon MCP](https://github.com/neondatabase/mcp-server-neon) | Neon Postgres | `run_sql`, `run_sql_transaction`, `prepare_/complete_database_migration`, branch/project mgmt | remote `https://mcp.neon.tech/mcp` (OAuth) | migrations on temp branch; `roleName` scoping |
| [Snowflake-Labs/mcp](https://github.com/snowflake-labs/mcp) | Snowflake | SQL execution, semantic view tools, other services (per README/issues) | config-file based | `sql_statement_permissions` per statement type |

The research describes the bar for "dead-simple" setup as:

- one copy-paste line (`claude mcp add ... -- npx -y pkg --dsn ...`) or a remote URL;
- a single DSN;
- read-only by default;
- 2 to 4 tools;
- a one-click bundle for Claude Desktop;
- one-click Cursor and VS Code links.

Postgres dominates demand. MySQL/MariaDB, SQLite and SQL Server come next. Each analytics warehouse (ClickHouse, Snowflake, BigQuery, DuckDB/MotherDuck) has an official vendor server, and limits on cost and bytes scanned are the warehouse-specific need.

### Framework tool interface shapes

| Framework | Tool shape | Input | Output / errors | Adapter notes |
|---|---|---|---|---|
| LangChain / LangGraph | `langchain_core.tools.BaseTool` subclass (`name`, `description`, `args_schema: type[BaseModel]`, `_run`, `_arun`) or `@tool`. Toolkits expose `get_tools() -> list[BaseTool]` | Pydantic args; SQL toolkit uses `query: str` and comma-separated `table_names: str` | `str`, or `(content, artifact)` with `response_format="content_and_artifact"`. `handle_tool_error` (bool/str/callable) turns `ToolException` into an observation | A toolkit whose tool names match `sql_db_*` allows drop-in replacement. Implement `_arun`. Return rows plus a `truncated` flag as the artifact |
| LlamaIndex | `FunctionTool.from_defaults(fn=..., async_fn=..., name=, description=, fn_schema=)`; `QueryEngineTool`; `BaseToolSpec` with `spec_functions` and `.to_tool_list()` (e.g. `DatabaseToolSpec`: `list_tables`, `describe_tables`, `load_data`) | Python signature or Pydantic `fn_schema` | `ToolOutput` (`content`, `raw_output`, `is_error`). Unknown returns are stringified with `is_error=False` (#22943), so raise or return explicit errors | A `BaseToolSpec` subclass, plus optionally a `BaseQueryEngine` subclass for `QueryEngineTool` |
| OpenAI Agents SDK | `@function_tool` on a sync or async function, or `FunctionTool(name, description, params_json_schema, on_invoke_tool(ctx, args_json))`. Supports `needs_approval`, `tool_input_guardrails` / `tool_output_guardrails`, `failure_error_function` | JSON schema from type hints and docstring | Any value, stringified. Guardrail behaviours: allow / reject_content / raise | Validation can be exposed as a tool input guardrail (rejects bad SQL before execution) as well as a function tool |
| CrewAI | `crewai.tools.BaseTool` subclass with `name`, `description`, `args_schema: type[BaseModel]`, `_run(...)`; or `@tool` | Pydantic | `str`. NL2SQLTool has an `allow_dml` flag and a `before_tool_call` hook | Mirroring the NL2SQLTool constructor (`db_uri`, `allow_dml`) keeps it familiar |
| Claude Agent SDK | `@tool(name, description, input_schema)` on an async handler; bundled with `create_sdk_mcp_server(name, version, tools=[...])` and passed via `ClaudeAgentOptions.mcp_servers`. Annotations such as `readOnlyHint` allow parallel calls | Dict of name to type, or a full JSON Schema | `{"content": [{"type": "text", "text": ...}], "is_error": bool}`. Return `is_error` rather than raising | An existing MCP server already fits. An in-process `create_sdk_mcp_server` factory and `readOnlyHint` on read tools are the additions |

Sources for the interface table:

- LangChain: [tools](https://github.com/langchain-ai/langchain/blob/afafd55d34cb11b794781fd0f7bef9127c633f84/openwiki/tools.md), [LangGraph SQL agent tools](https://github.com/langchain-ai/docs/blob/332b12ee879b1d04ec47dec765627c009eb84b7a/src/snippets/code-samples/langgraph-sql-agent-tools-py.mdx), [SQLDatabaseToolkit reference](https://reference.langchain.com/python/langchain-community/agent_toolkits/sql/toolkit/SQLDatabaseToolkit).
- LlamaIndex: [tools guide](https://github.com/run-llama/llama_index/blob/619d74664e8404612038bc15a1a40c686037277c/docs/src/content/docs/framework/module_guides/deploying/agents/tools.md), [llama-index-tools-database](https://llamahub.ai/l/tools/llama-index-tools-database?from=), [agents course](https://github.com/huggingface/agents-course/blob/3c469e773ec04e5f338b727ff20e3a6ea823ff81/units/en/unit2/llama-index/tools.mdx).
- OpenAI Agents SDK: [guardrails](https://github.com/openai/openai-agents-python/blob/64cd5dfc5a1579870dcbed0ea42b27aa84033fee/docs/guardrails.md), [function tools crash course](https://github.com/shubhamsaboo/awesome-llm-apps/blob/874bc13a57aa2ba1266a84f2444808ce3093efb6/ai_agent_framework_crash_course/openai_sdk_crash_course/3_tool_using_agent/3_1_function_tools/README.md).
- CrewAI: [custom tools](https://docs.crewai.com/edge/en/learn/create-custom-tools), [NL2SQLTool](https://github.com/crewaiinc/crewai/blob/4f4573706ea4afee6c4da4c31cfb9622a4495ab7/docs/edge/en/tools/database-data/nl2sqltool.mdx).
- Claude Agent SDK: [Python SDK](https://code.claude.com/docs/en/agent-sdk/python), [custom tools](https://github.com/pjbrito/claude-code-docs/blob/7ed14253bbdd17b3d465554be0d120e9abe9ae1c/content/en/agent-sdk/custom-tools.md), [claude-agent-sdk-python #313](https://github.com/anthropics/claude-agent-sdk-python/issues/313).

The de facto tool set is LangChain's four tools: `sql_db_list_tables`, `sql_db_schema`, `sql_db_query_checker` and `sql_db_query`. Other projects clone it, for example the [IBM watsonx SQL docs](https://github.com/langchain-ai/docs/blob/332b12ee879b1d04ec47dec765627c009eb84b7a/src/oss/python/integrations/tools/ibm_watsonx_sql.mdx). Agno also ships [SQLTools](https://github.com/agno-agi/docs/blob/1c1bd661701a2891ec352e8855a2b83b64e5a24b/tools/toolkits/database/sql.mdx).

---

## Implications for QueryPilot

See docs/roadmap.md for the gap analysis and prioritized roadmap.
