from __future__ import annotations

import argparse
import json
import os
import sys

from querypilot.access import AccessPolicy
from querypilot import QueryPilot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="querypilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI QueryPilot server.")
    _add_runtime_args(serve_parser)
    serve_parser.add_argument("--host", default=os.getenv("QUERYPILOT_HOST", "127.0.0.1"))
    serve_parser.add_argument("--port", type=int, default=int(os.getenv("QUERYPILOT_PORT", "8000")))

    mcp_parser = subparsers.add_parser("mcp", help="Run the QueryPilot MCP server.")
    _add_runtime_args(mcp_parser)
    mcp_parser.add_argument(
        "--transport",
        default=os.getenv("QUERYPILOT_MCP_TRANSPORT", "stdio"),
        choices=["stdio", "streamable-http", "sse"],
    )

    eval_parser = subparsers.add_parser("eval", help="Eval-driven SQL reliability harness.")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)

    eval_run_parser = eval_subparsers.add_parser(
        "run",
        help="Run a benchmark suite end-to-end and emit a SuiteReport.",
    )
    _add_eval_run_args(eval_run_parser)

    args = parser.parse_args(argv)

    if args.command == "serve":
        _serve(args)
        return 0
    if args.command == "mcp":
        _mcp(args)
        return 0
    if args.command == "eval":
        if args.eval_command == "run":
            return _eval_run(args)
        raise SystemExit(f"Unknown eval subcommand: {args.eval_command}")
    return 0


def _add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", default=os.getenv("QUERYPILOT_DATABASE_URL"))
    parser.add_argument("--dialect", default=os.getenv("QUERYPILOT_DIALECT", "sqlite"))
    parser.add_argument("--max-rows", type=int, default=int(os.getenv("QUERYPILOT_MAX_ROWS", "100")))
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("QUERYPILOT_TIMEOUT_SECONDS", "10")),
    )
    parser.add_argument(
        "--access-policy-json",
        default=os.getenv("QUERYPILOT_ACCESS_POLICY_JSON"),
        help="JSON object for AccessPolicy configuration.",
    )


def _add_eval_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--suite",
        required=True,
        help="Path to a suite YAML/JSON file or a directory of suite files.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("QUERYPILOT_DATABASE_URL"),
        help="Default database URL when a case does not set fixture_db.",
    )
    parser.add_argument("--dialect", default=os.getenv("QUERYPILOT_DIALECT", "sqlite"))
    parser.add_argument(
        "--generator",
        default="demo",
        choices=("demo", "openai", "anthropic"),
        help="SQL generator to evaluate. Use 'demo' for the offline deterministic generator.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the default model for openai/anthropic generators.",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Path to write the JSON SuiteReport. Terminal output is always printed.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers (>1 uses ThreadPoolExecutor; LLM rate limits apply).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=int(os.getenv("QUERYPILOT_MAX_ROWS", "100")),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("QUERYPILOT_TIMEOUT_SECONDS", "10")),
    )
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors in terminal output.")


def _serve(args: argparse.Namespace) -> None:
    if not args.database_url:
        raise SystemExit("--database-url or QUERYPILOT_DATABASE_URL is required.")
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install querypilot[server] to use `querypilot serve`.") from exc

    from querypilot.server.app import create_app

    app = create_app(
        database_url=args.database_url,
        dialect=args.dialect,
        max_rows=args.max_rows,
        timeout_seconds=args.timeout_seconds,
        access_policy=_access_policy_from_args(args),
    )
    uvicorn.run(app, host=args.host, port=args.port)


def _mcp(args: argparse.Namespace) -> None:
    if not args.database_url:
        raise SystemExit("--database-url or QUERYPILOT_DATABASE_URL is required.")

    from querypilot.mcp.server import create_mcp_server

    qp = QueryPilot.connect(
        database_url=args.database_url,
        dialect=args.dialect,
        max_rows=args.max_rows,
        timeout_seconds=args.timeout_seconds,
        access_policy=_access_policy_from_args(args),
    )
    server = create_mcp_server(qp)
    server.run(transport=args.transport)


def _eval_run(args: argparse.Namespace) -> int:
    from querypilot.evals.factory import (
        build_cost_tracker_factory,
        build_generator,
        build_qp_factory,
        load_suite_or_dir,
    )
    from querypilot.evals.report import render_terminal, write_json
    from querypilot.evals.suite_runner import run_suite

    try:
        suite = load_suite_or_dir(args.suite)
    except Exception as exc:
        raise SystemExit(f"Failed to load suite at {args.suite}: {exc}") from exc

    if not args.database_url and suite.fixture_db is None:
        raise SystemExit(
            "--database-url is required when the suite does not declare fixture_db."
        )

    generator = build_generator(args.generator, model=args.model)
    tracker_factory = build_cost_tracker_factory(args.generator)
    qp_factory = build_qp_factory(
        database_url=args.database_url or suite.fixture_db or "",
        dialect=args.dialect,
        generator=generator,
        max_rows=args.max_rows,
        timeout_seconds=args.timeout_seconds,
    )

    report = run_suite(
        suite,
        qp_factory=qp_factory,
        cost_tracker_factory=tracker_factory,
        max_workers=args.workers,
        generator_name=args.generator,
        model_name=args.model,
        database_url=args.database_url,
    )

    if args.report:
        write_json(report, args.report)

    print(render_terminal(report, color=not args.no_color))
    return 0


def _access_policy_from_args(args: argparse.Namespace) -> AccessPolicy | None:
    if not args.access_policy_json:
        return None
    try:
        payload = json.loads(args.access_policy_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --access-policy-json: {exc}") from exc
    return AccessPolicy.model_validate(payload)


if __name__ == "__main__":
    sys.exit(main())
