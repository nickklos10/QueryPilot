# SaaSPulse — the QueryPilot native benchmark

SaaSPulse is a **contamination-proof, execution-truth text-to-SQL benchmark**
built and owned by QueryPilot. It measures the four axes public leaderboards
(Spider, BIRD) don't report together: **correctness** (by executing SQL, not
string-matching), **safety** (does the model refuse destructive SQL?),
**latency**, and **$ cost per query**.

Its headline property is that the schema and data **never existed on the public
web**, so no model was trained on it. "The model just memorized Spider" is not a
possible objection to a result measured here.

```
benchmarks/
  README.md                 # this file
  validate_golds.py         # pre-flight: execute every gold, block every unsafe case
  fixtures/
    make_saaspulse.py       # deterministic fixture generator (fixed seed)
    saaspulse.db            # generated SQLite DB (committed, ~3.6 MB)
  saaspulse/
    correctness.yaml        # 65 correctness cases (easy / medium / hard)
    safety.yaml             # 37 safety cases (should_pass: false)
```

## The schema

A realistic B2B-SaaS analytics database (8 tables, foreign keys, NULLs, and
edge rows), the exact shape of the "analytics agent" workload QueryPilot targets:

| Table | Grain | Notable edges |
|---|---|---|
| `plans` | subscription plan catalog | two `$0` plans (Trial, Legacy Free); `Enterprise` has a NULL (unlimited) `seat_limit`; one retired plan |
| `accounts` | customer companies | `churned_date` NULL for live accounts; `segment` ∈ {smb, midmarket, enterprise} |
| `users` | users per account (FK → accounts) | `last_login_at` NULL for users who never logged in |
| `subscriptions` | an account's plan over time (FK → accounts, plans) | `ended_at` NULL while active; `mrr_cents` = 0 for free/trial; statuses active/cancelled/past_due/trialing |
| `invoices` | issued invoices (FK → accounts, subscriptions) | `$0` invoices; statuses paid/open/void/uncollectible; multi-currency |
| `payments` | payments against invoices (FK → invoices, accounts) | open/void/uncollectible/`$0` invoices have **no** payment row (LEFT-JOIN edges) |
| `support_tickets` | tickets per account (FK → accounts) | `closed_at` NULL for open tickets; `satisfaction_score` NULL when unrated |
| `usage_events` | product usage (FK → accounts, users) | the bulk table (~60k rows); power-law distribution across accounts |

All dates are stored as **timezone-less ISO strings** (`YYYY-MM-DD` for dates,
`YYYY-MM-DD HH:MM:SS` for timestamps). Money is integer cents.

## Case composition (102 cases)

**Correctness — 65 cases**, tagged by difficulty tier:

| Tier | Count | Shape |
|---|---|---|
| `easy` | 26 | single-table filters, projections, simple aggregates |
| `medium` | 23 | 2–3 table joins, GROUP BY, HAVING, month bucketing |
| `hard` | 16 | subqueries, anti/semi joins, conditional aggregation, date arithmetic, OFFSET ranking |

Every question **fully determines its answer** — it names the exact output
columns (matching the gold aliases), each table's interpretation, all filters,
the ordering with a deterministic tiebreaker, and any row limit. This is
deliberate: an ambiguous question lets a model return a defensibly-different-but-
"wrong" result and poisons the score. Gold SQL is boring, canonical SQLite.

**Safety — 37 cases** (`should_pass: false`), spanning DDL (`DROP`/`CREATE`/
`ALTER`/`TRUNCATE`/`VACUUM`/`GRANT`), DML (`DELETE`/`UPDATE`/`INSERT`/`REPLACE`/
upsert), multi-statement smuggling, comment-smuggled injection, CTE-hidden
mutations, `PRAGMA`/`ATTACH`, blocked privileged functions (`pg_read_file`,
`pg_sleep`, `nextval`, `pg_terminate_backend`), Cartesian joins, and
`sqlite_master` schema exfiltration.

Scoring is **row-order-insensitive with float tolerance** (`comparison` block in
each suite YAML). Because every question names its exact output columns,
name-sensitive column comparison is intentional here.

## Regenerating the fixture

The generator is fully deterministic — a single fixed seed drives every choice,
all dates derive from a fixed epoch (never `datetime.now()`), and the DB is
`VACUUM`-ed for a stable page layout. Two runs produce **byte-identical** files.

```bash
python benchmarks/fixtures/make_saaspulse.py          # writes benchmarks/fixtures/saaspulse.db

# prove byte-stability:
python benchmarks/fixtures/make_saaspulse.py --out /tmp/a.db --quiet
python benchmarks/fixtures/make_saaspulse.py --out /tmp/b.db --quiet
shasum -a 256 /tmp/a.db /tmp/b.db                     # the two hashes must match
```

`saaspulse.db` is committed (like `tests/fixtures/demo.db`) so the suite runs
without a generation step. Regenerate and re-commit it only when you change the
generator, and always re-run the validator afterward:

```bash
python benchmarks/validate_golds.py
```

`validate_golds.py` executes every gold query against the fixture (failing on any
error or unexpected empty result set) and pushes every safety case through the
real validator (failing unless it is blocked for the right reason). Run it in CI
and after any fixture or suite edit.

## Running the model matrix

Run the suite once per model, writing one `SuiteReport` JSON each, then aggregate
them into a leaderboard.

```bash
# Offline smoke check (no API key; demo fails correctness, blocks all unsafe SQL):
querypilot eval run --suite benchmarks/saaspulse --generator demo \
  --report reports/demo.json

# One report per model:
querypilot eval run --suite benchmarks/saaspulse \
  --generator anthropic --model claude-opus-4-8 --report reports/opus.json
querypilot eval run --suite benchmarks/saaspulse \
  --generator openai --model gpt-5.1 --report reports/gpt51.json

# Open weights at $0 marginal cost via an OpenAI-compatible endpoint:
querypilot eval run --suite benchmarks/saaspulse \
  --generator openai-compatible --base-url http://localhost:11434/v1 \
  --model qwen2.5-coder --report reports/qwen.json

# Ranked comparison across all reports:
querypilot eval leaderboard --report reports/ --format md --output reports/leaderboard.md
```

`eval run` passes the suite directory straight through: `correctness.yaml` and
`safety.yaml` are merged into one run (they share `fixture_db`, `fixture_dialect`,
`thresholds`, and `comparison`). The `--report` JSON contains per-case rows —
question, gold SQL, candidate SQL, pass/fail, failure category, latency, tokens,
and cost — which is the anti-cherry-pick evidence a published benchmark needs.

## Why this is contamination-proof

Public text-to-SQL leaderboards are contaminated: their databases and gold SQL
are on the web and in training data. SaaSPulse defends the results four ways:

1. **Novel schema.** It was authored for this benchmark and never published, so
   no model memorized its questions or answers.
2. **Safety and cost are contamination-immune anyway.** Memorizing gold SQL
   doesn't make a model refuse `DROP TABLE` or cost fewer dollars — the safety
   and `$/query` columns stand regardless of training exposure.
3. **Execution truth, not string match.** Correctness is scored by running both
   the gold and the candidate and comparing result sets, so a model can't win by
   reproducing a memorized query string; it has to produce the right *rows*.
4. **Fully reproducible.** The fixture is deterministic and committed, the gold
   SQL and questions are in plain YAML, and `validate_golds.py` proves the whole
   suite is executable — "here's the raw data, run it yourself" is the defense.
