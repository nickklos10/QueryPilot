"""Quickstart CLI commands: demo, schema, sql, ask, init and mcp-config."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from querypilot.access import AccessPolicy
from querypilot.project import (
    GeneratorSettings,
    ProjectConfig,
    connect_project,
    find_config,
    load_project_config,
    resolve_generator_name,
)

MCP_CLIENTS = ("claude-code", "claude-desktop", "cursor", "vscode", "windsurf")
DEMO_DB_FILENAME = "querypilot-demo.db"
_MAX_CELL_WIDTH = 40


def add_connection_args(parser: argparse.ArgumentParser, *, generator: bool = True) -> None:
    """Flags shared by every command that talks to a database."""
    parser.add_argument(
        "--db",
        "--database-url",
        dest="database_url",
        default=os.getenv("QUERYPILOT_DATABASE_URL"),
        help="SQLAlchemy database URL, e.g. sqlite:///app.db or postgresql://user@host/db.",
    )
    parser.add_argument(
        "--dialect",
        default=os.getenv("QUERYPILOT_DIALECT"),
        help="SQL dialect; inferred from the URL when omitted.",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("QUERYPILOT_CONFIG"),
        help="Project config file (default: ./querypilot.yaml when present).",
    )
    parser.add_argument("--max-rows", type=int, default=_env_int("QUERYPILOT_MAX_ROWS"))
    parser.add_argument("--timeout-seconds", type=int, default=_env_int("QUERYPILOT_TIMEOUT_SECONDS"))
    parser.add_argument(
        "--access-policy-json",
        default=os.getenv("QUERYPILOT_ACCESS_POLICY_JSON"),
        help="JSON object for AccessPolicy configuration.",
    )
    parser.add_argument(
        "--hint",
        action="append",
        default=None,
        help="Business rule or glossary entry for the model (repeatable).",
    )
    if generator:
        parser.add_argument(
            "--generator",
            default=os.getenv("QUERYPILOT_GENERATOR"),
            choices=["auto", "demo", "openai", "anthropic", "openai-compatible"],
            help="SQL generator for ask/ask_database (default: auto, from API keys in the environment).",
        )
        parser.add_argument("--model", default=os.getenv("QUERYPILOT_MODEL"))
        parser.add_argument("--base-url", default=os.getenv("QUERYPILOT_BASE_URL"))


def connect_from_args(args: argparse.Namespace):
    """Build a QueryPilot client from CLI flags layered over querypilot.yaml."""
    config = _project_config(args)
    generator_settings = config.generator.model_copy()
    if getattr(args, "generator", None):
        generator_settings.provider = args.generator
    if getattr(args, "model", None):
        generator_settings.model = args.model
    if getattr(args, "base_url", None):
        generator_settings.base_url = args.base_url
    config.generator = generator_settings
    if args.hint:
        config.hints = [*config.hints, *args.hint]
    try:
        return connect_project(
            config,
            database_url=args.database_url,
            dialect=args.dialect,
            max_rows=args.max_rows,
            timeout_seconds=args.timeout_seconds,
            access_policy=_access_policy(args.access_policy_json),
        )
    except (ValueError, ImportError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc


def run_demo(args: argparse.Namespace) -> int:
    from querypilot.demo import create_demo_db

    path = Path(args.path)
    create_demo_db(path)
    url = f"sqlite:///{path}"
    print(f"Created a demo database at {path}\n")

    from querypilot.core.client import QueryPilot

    qp = QueryPilot.connect(url)
    sql = "SELECT customer_name, segment, arr FROM customers ORDER BY arr DESC"
    print(f"$ querypilot sql --db {url} {shlex.quote(sql)}")
    result = qp.execute_sql(sql)
    print(f"-- rewritten by the validator: {result.sql}")
    print(format_table(result.rows))

    blocked = "DELETE FROM customers"
    print(f"\n$ querypilot sql --db {url} {shlex.quote(blocked)}")
    validation = qp.validate_sql(blocked)
    print("Blocked: " + "; ".join(validation.errors))

    print(
        "\nNext steps:\n"
        f"  querypilot schema --db {url}\n"
        f"  querypilot ask --db {url} \"Which customers have the highest ARR?\"   "
        "(set OPENAI_API_KEY or ANTHROPIC_API_KEY)\n"
        f"  querypilot mcp-config claude-code --db sqlite:///{path.resolve()}"
    )
    return 0


def run_schema(args: argparse.Namespace) -> int:
    qp = connect_from_args(args)
    toolkit = qp.toolkit()
    if args.tables:
        payload = toolkit.dispatch("describe_table", {"table_names": args.tables})
    else:
        payload = toolkit.dispatch("list_tables", {})
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if not payload.get("ok", True):
        print(f"Error: {payload.get('error')}", file=sys.stderr)
        return 1
    if args.tables:
        for table in payload.get("tables", []):
            print(_render_table_details(table))
    else:
        print(format_table(payload.get("tables", [])))
    return 0


def run_sql(args: argparse.Namespace) -> int:
    qp = connect_from_args(args)
    tool = "validate_sql" if args.validate_only else "run_sql"
    payload = qp.toolkit().dispatch(tool, {"sql": args.sql})
    return _print_payload(payload, args.json)


def run_ask(args: argparse.Namespace) -> int:
    qp = connect_from_args(args)
    if resolve_generator_name(getattr(args, "generator", None) or _config_provider(args)) == "demo":
        print(
            "Note: no LLM configured, so the offline demo generator is answering "
            "(it only handles a few templates). Set OPENAI_API_KEY or ANTHROPIC_API_KEY, "
            "or pass --generator.",
            file=sys.stderr,
        )
    try:
        answer = qp.ask(args.question)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(answer.model_dump(), indent=2, default=str))
        return 0
    print(f"SQL: {answer.sql}\n")
    print(format_table(answer.rows))
    return 0


def run_init(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if target.exists() and not args.force:
        raise SystemExit(f"{target} already exists; pass --force to overwrite.")
    tables: list[str] = []
    if args.database_url:
        from querypilot.core.client import QueryPilot

        try:
            qp = QueryPilot.connect(args.database_url, dialect=args.dialect)
            tables = [table.name for table in qp.get_schema().tables]
        except Exception as exc:  # noqa: BLE001 - init should still write a config
            print(f"Warning: could not introspect the database: {exc}", file=sys.stderr)
    target.write_text(render_config_template(args.database_url, tables))
    print(f"Wrote {target}. Every querypilot command in this directory now reads it.")
    if args.database_url and "@" in args.database_url:
        print("Tip: replace credentials in database_url with ${ENV_VAR} references before committing.")
    return 0


def run_mcp_config(args: argparse.Namespace) -> int:
    database_url = _absolute_sqlite_url(args.database_url or "sqlite:///path/to/your.db")
    command, command_args = _mcp_launch_command(args.runner)
    env = {"QUERYPILOT_DATABASE_URL": database_url}
    if args.generator:
        env["QUERYPILOT_GENERATOR"] = args.generator
    print(render_mcp_config(args.client, command, command_args, env))
    if args.runner == "uvx" and not shutil.which("uvx"):
        print(
            "\nNote: uvx was not found on PATH. Install uv (https://docs.astral.sh/uv/) "
            "or use --runner python to launch this Python environment directly.",
            file=sys.stderr,
        )
    return 0


def render_mcp_config(client: str, command: str, command_args: list[str], env: dict[str, str]) -> str:
    if client == "claude-code":
        env_flags = " ".join(f"--env {key}={shlex.quote(value)}" for key, value in env.items())
        launch = " ".join(shlex.quote(part) for part in [command, *command_args])
        return f"claude mcp add querypilot {env_flags} -- {launch}"
    server = {"command": command, "args": command_args, "env": env}
    if client == "vscode":
        return json.dumps({"servers": {"querypilot": {"type": "stdio", **server}}}, indent=2)
    return json.dumps({"mcpServers": {"querypilot": server}}, indent=2)


def render_config_template(database_url: str | None, tables: list[str]) -> str:
    lines = [
        "# QueryPilot project config. The querypilot CLI, `querypilot mcp`, `querypilot serve`",
        "# and QueryPilot.from_config() read this file. ${VAR} values come from the environment.",
        f"database_url: {json.dumps(database_url or '${QUERYPILOT_DATABASE_URL}')}",
        "max_rows: 100",
        "",
        "# Business rules the model should follow, e.g.",
        '#   - "Revenue means SUM(amount_cents) / 100 over invoices with status = \'paid\'"',
        "hints: []",
        "",
        "# Plain-language descriptions keyed by table or table.column. Blank entries are ignored.",
        "descriptions:",
    ]
    lines.extend(f"  {table}: \"\"" for table in tables)
    if not tables:
        lines[-1] = "descriptions: {}"
    lines.extend(
        [
            "",
            "# Restrict what agents can see.",
            "# blocked_tables: [audit_log]",
            "# access_policy:",
            "#   blocked_columns: {users: [password_hash]}",
            "#   masking_rules: {users: {email: {mode: redact}}}",
            "#   row_filters: {orders: \"tenant_id = 42\"}",
            "",
            "generator:",
            "  provider: auto   # auto | openai | anthropic | openai-compatible | demo",
            "  # model: gpt-5.1",
            "",
        ]
    )
    return "\n".join(lines)


def format_table(rows: list[dict[str, Any]]) -> str:
    """Render rows as a fixed-width text table."""
    if not rows:
        return "(no rows)"
    columns = list(rows[0].keys())
    cells = [[_cell(row.get(column)) for column in columns] for row in rows]
    widths = [
        max(len(column), *(len(row[index]) for row in cells)) for index, column in enumerate(columns)
    ]
    header = "  ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    rule = "  ".join("-" * width for width in widths)
    body = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in cells]
    count = f"({len(rows)} row{'s' if len(rows) != 1 else ''})"
    return "\n".join([header, rule, *body, count])


def _print_payload(payload: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return 0 if payload.get("ok") else 1
    if not payload.get("ok"):
        print(f"Error: {payload.get('error')}", file=sys.stderr)
        for hint in payload.get("hints", []):
            print(f"  hint: {hint}", file=sys.stderr)
        return 1
    if "rows" in payload:
        columns = payload.get("columns", [])
        rows = [dict(zip(columns, row, strict=False)) for row in payload["rows"]]
        print(format_table(rows))
        if payload.get("truncated"):
            print(payload.get("note") or "Result truncated at max_rows.")
    else:
        print(f"Valid. Runs as: {payload.get('sql')}")
    for warning in payload.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _render_table_details(table: dict[str, Any]) -> str:
    title = table["name"] + (" (view)" if table.get("is_view") else "")
    lines = [title + (f"  -- {table['comment']}" if table.get("comment") else "")]
    for column in table.get("columns", []):
        parts = [f"  {column['name']} {column.get('type', '')}".rstrip()]
        if column.get("primary_key"):
            parts.append("PK")
        if column.get("comment"):
            parts.append(f"-- {column['comment']}")
        if column.get("sample_values"):
            parts.append("e.g. " + ", ".join(repr(value) for value in column["sample_values"]))
        lines.append(" ".join(parts))
    for foreign_key in table.get("foreign_keys", []):
        lines.append(f"  FK {foreign_key}")
    return "\n".join(lines) + "\n"


def _absolute_sqlite_url(database_url: str) -> str:
    # MCP clients launch servers from their own working directory, so relative paths break.
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.startswith(prefix + "/"):
        return database_url
    path = database_url[len(prefix) :]
    if path in {"", ":memory:"} or path.startswith("path/to/"):
        return database_url
    return prefix + str(Path(path).expanduser().resolve())


def _mcp_launch_command(runner: str) -> tuple[str, list[str]]:
    if runner == "python":
        return sys.executable, ["-m", "querypilot", "mcp"]
    # An absolute path avoids "spawn uvx ENOENT" in GUI clients that don't inherit the shell PATH.
    uvx = shutil.which("uvx") or "uvx"
    return uvx, ["--from", "querypilot[mcp]", "querypilot", "mcp"]


def _project_config(args: argparse.Namespace) -> ProjectConfig:
    path = Path(args.config) if getattr(args, "config", None) else find_config()
    if path is None:
        return ProjectConfig(generator=GeneratorSettings())
    if not path.is_file():
        raise SystemExit(f"Config file not found: {path}")
    try:
        return load_project_config(path)
    except (ValueError, ImportError) as exc:
        raise SystemExit(f"Invalid config {path}: {exc}") from exc


def _config_provider(args: argparse.Namespace) -> str | None:
    try:
        return _project_config(args).generator.provider
    except SystemExit:
        return None


def _access_policy(raw: str | None) -> AccessPolicy | None:
    if not raw:
        return None
    try:
        return AccessPolicy.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"Invalid --access-policy-json: {exc}") from exc


def _env_int(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None


def _cell(value: Any) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= _MAX_CELL_WIDTH else text[: _MAX_CELL_WIDTH - 1] + "…"
