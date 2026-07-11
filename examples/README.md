# QueryPilot examples

Runnable, self-contained examples covering QueryPilot's core surface. Every
example uses the bundled demo SQLite fixture (`tests/fixtures/demo.db`), so
none of them need a real production database.

## Setup

```bash
# From the repo root
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,eval]"

# Seed the demo fixture if it is missing
.venv/bin/python tests/fixtures/seed_demo.py
```

Then run any example from the repo root, e.g.:

```bash
.venv/bin/python examples/01_quickstart.py
```

## Index

| Example | What it shows | How to run | Needs a key? |
| --- | --- | --- | --- |
| [`01_quickstart.py`](01_quickstart.py) | Connect to the demo fixture, `execute_sql`, offline `ask()`, print validation risk level | `python examples/01_quickstart.py` | No |
| [`02_openai_tool_use.py`](02_openai_tool_use.py) | `as_openai_tools()` wired into an OpenAI (`gpt-5.1`) tool-use loop | `pip install -e ".[openai]"` then `python examples/02_openai_tool_use.py` | `OPENAI_API_KEY` |
| [`03_anthropic_tool_use.py`](03_anthropic_tool_use.py) | `as_anthropic_tools()` wired into an Anthropic (`claude-sonnet-5`) tool-use loop | `pip install -e ".[anthropic]"` then `python examples/03_anthropic_tool_use.py` | `ANTHROPIC_API_KEY` |
| [`04_access_control.py`](04_access_control.py) | `AccessPolicy`: blocked columns, row filter, masking — a rejected query and a masked result | `python examples/04_access_control.py` | No |
| [`05_custom_eval_suite/`](05_custom_eval_suite/) | A custom YAML suite (3 correctness + 1 safety case) run with `querypilot eval run` / `check` | `bash examples/05_custom_eval_suite/run.sh` | No |
| [`06_mcp/`](06_mcp/) | Run `querypilot mcp` and a paste-ready Claude Desktop / Claude Code MCP config | see [`06_mcp/README.md`](06_mcp/README.md) | No |

Examples 02 and 03 build the QueryPilot connection and tool schemas *before*
their API-key guard, so you can confirm the schema wiring even without a key —
they exit with a clear message when the key is absent.

`_common.py` is a tiny shared helper that resolves the demo fixture path; it is
not part of QueryPilot's public API.
