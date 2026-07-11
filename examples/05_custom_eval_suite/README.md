# 05 - Custom eval suite

A small hand-authored benchmark suite (`suite.yaml`) with three correctness
cases and one safety case, run against the bundled demo SQLite fixture. No API
keys are required — the `demo` generator is deterministic and offline.

## Run it

```bash
pip install -e ".[eval]"        # PyYAML for suite loading
bash examples/05_custom_eval_suite/run.sh
```

Or invoke the two steps directly:

```bash
cd examples/05_custom_eval_suite

# Run the suite; write a JSON SuiteReport. Terminal report is always printed.
querypilot eval run --suite suite.yaml --generator demo --report out.json

# Gate the report against thresholds. Exits non-zero on any violation.
querypilot eval check --report out.json --threshold 0.9 --require-safety 1.0
```

## What the suite checks

| Case                     | Kind        | What it proves                                        |
| ------------------------ | ----------- | ----------------------------------------------------- |
| `top_customers_by_revenue` | correctness | `gold_sql` rows match the generated SQL's rows        |
| `customer_count`         | correctness | aggregate result matches                              |
| `top_customers_by_arr`   | correctness | ranking + column expectations                         |
| `blocks_drop_table`      | safety      | a DDL candidate is rejected by the validator          |

`fixture_db` inside `suite.yaml` is resolved relative to the suite file, so the
suite runs from any working directory.

## CI gate

Commit a baseline on your main branch and gate future runs against it:

```bash
querypilot eval run --suite suite.yaml --generator demo --report baseline.json
# ...later, on a PR...
querypilot eval check --report out.json --baseline baseline.json \
    --threshold 0.9 --require-safety 1.0
```

To benchmark a real generator instead of `demo`, use `--generator openai`
or `--generator anthropic` (requires the matching extra and API key).
