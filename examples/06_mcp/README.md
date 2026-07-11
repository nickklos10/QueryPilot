# 06 - MCP server

Run QueryPilot as an MCP tool server so any MCP client (Claude Desktop, Claude
Code, etc.) can safely query a database through the four QueryPilot tools:
`ask_database`, `search_schema`, `validate_sql`, `execute_sql`.

## Run the server

```bash
pip install -e ".[mcp]"
querypilot mcp --database-url sqlite:///demo.db --dialect sqlite
```

By default the server uses **stdio** transport (what Claude Desktop and Claude
Code expect). For clients that speak Streamable HTTP:

```bash
querypilot mcp \
  --database-url sqlite:///demo.db \
  --dialect sqlite \
  --transport streamable-http
```

You can supply the database via environment variables instead of flags:

```bash
export QUERYPILOT_DATABASE_URL=sqlite:///demo.db
export QUERYPILOT_DIALECT=sqlite
querypilot mcp
```

An optional access policy can be passed as JSON (see `examples/04_access_control.py`
for what the fields do):

```bash
querypilot mcp \
  --database-url sqlite:///demo.db \
  --access-policy-json '{"blocked_columns": {"customers": ["arr"]}}'
```

## Claude Desktop config

Add this to your `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`).
Use an **absolute** SQLite path — note the four slashes (`sqlite:////` + `/abs/path`):

```json
{
  "mcpServers": {
    "querypilot": {
      "command": "querypilot",
      "args": [
        "mcp",
        "--database-url",
        "sqlite:////absolute/path/to/tests/fixtures/demo.db",
        "--dialect",
        "sqlite"
      ]
    }
  }
}
```

If `querypilot` is not on the global PATH, point `command` at the venv binary
(e.g. `/path/to/.venv/bin/querypilot`).

## Claude Code config

Register the same server from the CLI:

```bash
claude mcp add querypilot -- \
  querypilot mcp \
  --database-url sqlite:////absolute/path/to/tests/fixtures/demo.db \
  --dialect sqlite
```

Or commit a project-scoped `.mcp.json` at your repo root:

```json
{
  "mcpServers": {
    "querypilot": {
      "command": "querypilot",
      "args": [
        "mcp",
        "--database-url",
        "sqlite:////absolute/path/to/tests/fixtures/demo.db",
        "--dialect",
        "sqlite"
      ]
    }
  }
}
```

Once connected, ask the agent a data question and it will route through
QueryPilot's validation + execution rather than touching the database directly.
