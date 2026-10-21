from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from querypilot.trace.hooks import SCOPES, default_audit_log

DEFAULT_TRANSCRIPTS = "~/.claude/projects"


def add_trace_parser(subparsers) -> None:
    trace_parser = subparsers.add_parser(
        "trace", help="Import and diagnose the SQL an agent ran through MCP or connector tools."
    )
    trace_sub = trace_parser.add_subparsers(dest="trace_command", required=True)

    import_parser = trace_sub.add_parser(
        "import", help="Read Claude Code transcripts into the QueryPilot audit log and schema cache."
    )
    import_parser.add_argument(
        "paths", nargs="*", default=[DEFAULT_TRANSCRIPTS], help="Transcript .jsonl files or directories."
    )
    import_parser.add_argument(
        "--output", default=None, help=f"Audit JSONL to append to (default: {_display(default_audit_log())})."
    )
    import_parser.add_argument("--cache", default=None, help="Schema cache file (default: ~/.querypilot/trace).")
    import_parser.add_argument("--no-cache", action="store_true", help="Do not update the schema cache.")
    _add_tool_arg(import_parser)

    doctor_parser = trace_sub.add_parser(
        "doctor", help="Report failure classes, retry chains, latency, and where each problem is best fixed."
    )
    doctor_parser.add_argument(
        "paths",
        nargs="*",
        help=f"Transcripts, directories or audit logs (default: {DEFAULT_TRANSCRIPTS}).",
    )
    doctor_parser.add_argument("--format", choices=("terminal", "md", "json"), default=None)
    doctor_parser.add_argument("--output", default=None, help="Write the report here (.md or .json).")
    doctor_parser.add_argument(
        "--no-redact",
        action="store_true",
        help="Show dataset names and literals in examples (reports are redacted by default).",
    )
    doctor_parser.add_argument("--title", default="QueryPilot trace doctor", help="Markdown report title.")
    doctor_parser.add_argument("--stall-seconds", type=float, default=60.0, help="Calls slower than this stall.")
    doctor_parser.add_argument(
        "--max-rows", type=int, default=200, help="Row cap assumed when judging oversized results."
    )
    doctor_parser.add_argument(
        "--export-replay",
        default=None,
        metavar="PATH",
        help="Write recovered retry chains as audit JSONL for `querypilot eval replay`.",
    )
    doctor_parser.add_argument(
        "--suite", default=None, metavar="PATH", help="Also build the replay suite (needs --fixture-db)."
    )
    doctor_parser.add_argument("--fixture-db", default=None, help="Database URL for the exported suite's cases.")
    _add_tool_arg(doctor_parser)


def add_hooks_parser(subparsers) -> None:
    hooks_parser = subparsers.add_parser(
        "hooks", help="Install Claude Code hooks that log (and optionally guard) query tool calls."
    )
    hooks_sub = hooks_parser.add_subparsers(dest="hooks_command", required=True)

    install = hooks_sub.add_parser("install", help="Add QueryPilot hooks to a Claude Code settings file.")
    _add_scope_args(install)
    _add_hook_options(install)
    install.add_argument("--dry-run", action="store_true", help="Print the settings instead of writing them.")

    uninstall = hooks_sub.add_parser("uninstall", help="Remove QueryPilot hooks from a settings file.")
    _add_scope_args(uninstall)

    run = hooks_sub.add_parser("run", help="Handle one hook event (Claude Code calls this).")
    run.add_argument("event", choices=("pre", "post", "post-failure"))
    _add_hook_options(run)


def run_trace(args: argparse.Namespace) -> int:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    if args.trace_command == "import":
        return _trace_import(args)
    if args.trace_command == "doctor":
        return _trace_doctor(args)
    raise SystemExit(f"Unknown trace subcommand: {args.trace_command}")


def run_hooks(args: argparse.Namespace) -> int:
    from querypilot.trace.hooks import (
        install_hooks,
        main_hook,
        options_from_args,
        settings_path,
        uninstall_hooks,
    )

    if args.hooks_command == "run":
        return main_hook(args.event, options_from_args(args))
    path = Path(args.settings).expanduser() if args.settings else settings_path(args.scope)
    if args.hooks_command == "uninstall":
        removed = uninstall_hooks(path)
        print(f"Removed {removed} QueryPilot hook(s) from {path}" if removed else f"No QueryPilot hooks in {path}")
        return 0
    try:
        options = options_from_args(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.dry_run:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp) / "settings.json"
            if path.is_file():
                preview.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            print(json.dumps(install_hooks(preview, options), indent=2))
        return 0
    install_hooks(path, options)
    mode = "logging + guard" if options.guard else "logging"
    print(f"Installed QueryPilot hooks ({mode}) in {path}")
    print(f"  audit log: {options.audit_log}")
    print("  tools: " + ", ".join(options.registry.tool_names()))
    print("Restart Claude Code (or run /hooks) to load them. Remove with: querypilot hooks uninstall")
    return 0


def _trace_import(args: argparse.Namespace) -> int:
    from querypilot.audit.sinks import JSONLAuditSink
    from querypilot.trace.schema_cache import SchemaCache
    from querypilot.trace.transcript import (
        QUERY,
        SCHEMA,
        load_calls,
        read_audit_log,
        to_audit_record,
    )

    registry = _registry(args)
    output = Path(args.output).expanduser() if args.output else default_audit_log()
    calls = load_calls(args.paths, registry)
    known = {call.call_id for call in read_audit_log(output, registry)} if output.is_file() else set()
    sink = JSONLAuditSink(output)
    written = 0
    for call in calls:
        if call.call_id in known:
            continue
        sink.write(to_audit_record(call))
        written += 1

    cache_note = ""
    if not args.no_cache:
        cache = SchemaCache.load(args.cache)
        for call in calls:
            if call.kind == SCHEMA and call.dataset is not None:
                cache.add(call.dataset)
            elif call.kind == QUERY and call.header and call.sql:
                cache.learn_from_select_star(call.sql, call.header, call.dialect)
        cache_note = f"; schema cache: {len(cache)} datasets at {cache.save(args.cache)}"
    queries = sum(call.kind == QUERY for call in calls)
    sessions = len({call.session_id for call in calls if call.kind == QUERY})
    print(
        f"Read {queries} query calls ({sessions} sessions) and {len(calls) - queries} schema calls; "
        f"wrote {written} new records to {output}{cache_note}"
    )
    return 0


def _trace_doctor(args: argparse.Namespace) -> int:
    from querypilot.trace.doctor import (
        diagnose,
        render_markdown,
        render_terminal,
        replay_records,
    )
    from querypilot.trace.transcript import load_calls

    paths = args.paths or [DEFAULT_TRANSCRIPTS]
    calls = load_calls(paths, _registry(args))
    if not any(call.kind == "query" for call in calls):
        print(f"No query tool calls found in {', '.join(paths)}.", file=sys.stderr)
        return 1
    report = diagnose(calls, stall_seconds=args.stall_seconds, max_rows=args.max_rows, redact=not args.no_redact)

    fmt = args.format or _infer_format(args.output) or "terminal"
    if fmt == "md":
        text = render_markdown(report, title=args.title)
    elif fmt == "json":
        text = report.to_json()
    else:
        text = render_terminal(report)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(render_terminal(report))
        print(f"\nWrote {fmt} report to {args.output}")
    else:
        print(text)

    if args.export_replay or args.suite:
        records = replay_records(calls)
        target = Path(args.export_replay or Path(args.suite).with_suffix(".replay.jsonl"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(record.model_dump_json() + "\n")
        print(f"Wrote {len(records)} recovered retry chains to {target}")
        if args.suite:
            if not args.fixture_db:
                raise SystemExit("--suite needs --fixture-db (the database the cases run against).")
            from querypilot.evals.loader import write_suite
            from querypilot.evals.replay import replay_from_jsonl

            suite = replay_from_jsonl(
                target, fixture_db=args.fixture_db, suite_name="trace_regressions", extra_tags=["trace"]
            )
            print(f"Wrote {len(suite.cases)} cases to {write_suite(suite, args.suite)}")
    return 0


def _registry(args):
    from querypilot.trace.tools import ToolRegistry

    try:
        return ToolRegistry.from_args(args.tool)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _add_tool_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tool",
        action="append",
        default=[],
        metavar="NAME=SQL_FIELD[:DIALECT[:ROW_LIMIT_FIELD]]",
        help=(
            "Another query tool to track, e.g. mcp__warehouse__run_query=query:snowflake "
            "(repeatable; NAME may be a glob). Foundry's SQL tool is always tracked."
        ),
    )


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope",
        choices=SCOPES,
        default="user",
        help="user: ~/.claude/settings.json (default); project: .claude/settings.json; "
        "local: .claude/settings.local.json.",
    )
    parser.add_argument("--settings", default=None, help="Explicit settings file (overrides --scope).")


def _add_hook_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audit-log", default=None, help=f"Audit JSONL (default: {_display(default_audit_log())}).")
    parser.add_argument("--cache", default=None, help="Schema cache file (default: ~/.querypilot/trace).")
    parser.add_argument(
        "--guard",
        action="store_true",
        help="Validate SQL against cached schemas before it runs and deny unknown columns/tables.",
    )
    parser.add_argument(
        "--max-rows", type=int, default=None, help="With --guard: deny queries without a row cap at or below this."
    )
    parser.add_argument(
        "--require-schema",
        action="store_true",
        help="With --guard: deny queries on datasets whose schema the agent has not fetched yet.",
    )
    _add_tool_arg(parser)


def _infer_format(path: str | None) -> str | None:
    if not path:
        return None
    suffix = Path(path).suffix.lower()
    return {".md": "md", ".markdown": "md", ".json": "json"}.get(suffix)


def _display(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
