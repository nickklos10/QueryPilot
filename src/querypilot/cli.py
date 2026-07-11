from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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

    eval_replay_parser = eval_subparsers.add_parser(
        "replay",
        help="Materialize a regression suite from an audit log.",
    )
    _add_eval_replay_args(eval_replay_parser)

    eval_check_parser = eval_subparsers.add_parser(
        "check",
        help="Compare a SuiteReport JSON against thresholds and a baseline.",
    )
    _add_eval_check_args(eval_check_parser)

    eval_import_parser = eval_subparsers.add_parser(
        "import",
        help="Convert a downloaded Spider/BIRD dev set into per-db benchmark suites.",
    )
    _add_eval_import_args(eval_import_parser)

    eval_leaderboard_parser = eval_subparsers.add_parser(
        "leaderboard",
        help="Rank N SuiteReport JSONs (same suite, different models) into a table.",
    )
    _add_eval_leaderboard_args(eval_leaderboard_parser)

    eval_init_parser = eval_subparsers.add_parser(
        "init",
        help="Scaffold suites/ and .eval/ in the current directory.",
    )
    eval_init_parser.add_argument(
        "--target",
        default=".",
        help="Directory to scaffold into (default: current working directory).",
    )
    eval_init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files.",
    )

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
        if args.eval_command == "replay":
            return _eval_replay(args)
        if args.eval_command == "check":
            return _eval_check(args)
        if args.eval_command == "import":
            return _eval_import(args)
        if args.eval_command == "leaderboard":
            return _eval_leaderboard(args)
        if args.eval_command == "init":
            return _eval_init(args)
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


def _add_eval_replay_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--audit-jsonl",
        required=True,
        help="Path to a JSONL audit log written by JSONLAuditSink.",
    )
    parser.add_argument(
        "--fixture-db",
        required=True,
        help="Database URL to attach to every replayed case (e.g. sqlite:///fixtures/demo.db).",
    )
    parser.add_argument(
        "--fixture-dialect",
        default=None,
        help="Override the dialect inferred from --fixture-db (sqlite/postgres/mysql/snowflake/bigquery/redshift).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Where to write the replayed suite (.yaml/.yml/.json).",
    )
    parser.add_argument(
        "--name",
        default="audit_replay",
        help="Suite name to embed in the output file.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum number of cases to materialize.",
    )
    parser.add_argument(
        "--include-masked",
        action="store_true",
        help="Include cases whose audit record applied a non-empty access policy.",
    )
    parser.add_argument(
        "--include-failures",
        action="store_true",
        help="Include cases that failed validation, execution, or had errors.",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include cases that returned zero rows.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Extra tag to attach to every replayed case (repeatable).",
    )


def _add_eval_check_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report",
        required=True,
        help="Path to a SuiteReport JSON written by `querypilot eval run --report ...`.",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Optional baseline SuiteReport JSON for regression comparison.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Minimum overall pass_rate (0..1).",
    )
    parser.add_argument(
        "--max-p95-ms",
        type=int,
        default=None,
        help="Maximum p95 case latency in milliseconds.",
    )
    parser.add_argument(
        "--require-safety",
        type=float,
        default=None,
        help="Minimum safety_pass_rate (0..1).",
    )
    parser.add_argument(
        "--require-correctness",
        type=float,
        default=None,
        help="Minimum correctness_rate (0..1).",
    )
    parser.add_argument(
        "--outcome-json",
        default=None,
        help="Optional path to write the structured CheckOutcome JSON.",
    )


def _add_eval_leaderboard_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        required=True,
        metavar="PATH",
        help=(
            "SuiteReport JSON to include (repeatable). A directory expands to "
            "its *.json files; glob patterns are also accepted."
        ),
    )
    parser.add_argument(
        "--labels",
        default=None,
        help=(
            "Comma-separated labels overriding the generator/model names, in "
            "report order. Must match the resolved report count."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow aggregating reports that span different suites.",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Optional path to write the leaderboard as a file (see --format).",
    )
    parser.add_argument(
        "--format",
        default=None,
        choices=("md", "json"),
        help=(
            "Writer for --output. Defaults to the file extension "
            "(.md -> md, .json -> json)."
        ),
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors in terminal output.",
    )


def _add_eval_import_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset",
        required=True,
        help="Path to an extracted Spider/BIRD dev directory (contains dev.json).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output directory for the per-db suite YAML files.",
    )
    parser.add_argument(
        "--format",
        default=None,
        choices=("spider", "bird"),
        help="Dataset format. Auto-detected from the directory layout when omitted.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of cases to import (skipped cases do not count).",
    )
    parser.add_argument(
        "--db",
        action="append",
        default=[],
        help="Only import cases for this db_id (repeatable).",
    )
    parser.add_argument(
        "--name-prefix",
        default=None,
        help="Override the id/suite-name prefix (default: spider_dev / bird_dev).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Error instead of warn+skip on missing fixtures or non-executable gold SQL.",
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


def _eval_check(args: argparse.Namespace) -> int:
    from querypilot.evals.check import (
        check_report,
        format_outcome,
        load_report,
        write_outcome,
    )

    report = load_report(args.report)
    baseline = load_report(args.baseline) if args.baseline else None

    outcome = check_report(
        report,
        baseline=baseline,
        threshold=args.threshold,
        max_p95_ms=args.max_p95_ms,
        require_safety_pass_rate=args.require_safety,
        require_correctness_rate=args.require_correctness,
    )

    if args.outcome_json:
        write_outcome(outcome, args.outcome_json)

    print(format_outcome(outcome))
    return 0 if outcome.ok else 1


def _eval_leaderboard(args: argparse.Namespace) -> int:
    from querypilot.evals.check import load_report
    from querypilot.evals.leaderboard import (
        LeaderboardError,
        build_leaderboard,
        render_markdown,
        render_terminal,
        write_json,
        write_markdown,
    )

    paths = _resolve_report_paths(args.report)
    if not paths:
        raise SystemExit("No report files matched the given --report values.")

    reports = [load_report(path) for path in paths]

    labels: list[str] | None = None
    if args.labels:
        labels = [item.strip() for item in args.labels.split(",")]

    try:
        board = build_leaderboard(reports, labels=labels, force=args.force)
    except LeaderboardError as exc:
        raise SystemExit(str(exc)) from exc

    if args.output:
        fmt = args.format or _infer_leaderboard_format(args.output)
        if fmt == "md":
            write_markdown(board, args.output)
        elif fmt == "json":
            write_json(board, args.output)
        else:
            raise SystemExit(
                f"Cannot infer output format from {args.output!r}; pass "
                f"--format md|json."
            )
        # A file was written; keep stdout as the readable terminal render.
        print(render_terminal(board, color=not args.no_color))
    elif args.format == "md":
        print(render_markdown(board))
    elif args.format == "json":
        print(board.model_dump_json(indent=2))
    else:
        print(render_terminal(board, color=not args.no_color))
    return 0


def _resolve_report_paths(values: list[str]) -> list[str]:
    import glob

    resolved: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = Path(value)
        if candidate.is_dir():
            matches = sorted(str(p) for p in candidate.glob("*.json"))
        elif any(ch in value for ch in "*?["):
            matches = sorted(glob.glob(value))
        else:
            matches = [value]
        for match in matches:
            if match not in seen:
                seen.add(match)
                resolved.append(match)
    return resolved


def _infer_leaderboard_format(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in (".md", ".markdown"):
        return "md"
    if suffix == ".json":
        return "json"
    return None


def _eval_init(args: argparse.Namespace) -> int:
    from querypilot.evals.init import scaffold

    target = Path(args.target).resolve()
    written = scaffold(target, force=args.force)
    print(f"Scaffolded {len(written)} files in {target}")
    for path in written:
        print(f"  {path.relative_to(target)}")
    return 0


def _eval_replay(args: argparse.Namespace) -> int:
    from querypilot.evals.loader import write_suite
    from querypilot.evals.replay import replay_from_jsonl

    suite = replay_from_jsonl(
        args.audit_jsonl,
        fixture_db=args.fixture_db,
        fixture_dialect=args.fixture_dialect,
        suite_name=args.name,
        only_successful=not args.include_failures,
        skip_masked=not args.include_masked,
        skip_empty_results=not args.include_empty,
        limit=args.limit,
        extra_tags=args.tag,
    )

    target = write_suite(suite, args.output)
    print(f"Wrote {len(suite.cases)} cases to {target}")
    return 0


def _eval_import(args: argparse.Namespace) -> int:
    from querypilot.evals.datasets import DatasetImportError, import_dataset

    try:
        result = import_dataset(
            args.dataset,
            args.output,
            dataset_format=args.format,
            limit=args.limit,
            db_ids=args.db or None,
            strict=args.strict,
            name_prefix=args.name_prefix,
        )
    except DatasetImportError as exc:
        raise SystemExit(f"Import failed: {exc}") from exc

    print(
        f"Imported {result.imported_cases} {result.dataset_format} cases into "
        f"{result.db_count} per-db suites at {result.output_dir}"
    )
    if result.skipped_missing_fixture:
        print(f"  Skipped {result.skipped_missing_fixture} cases (missing fixture db).")
    if result.skipped_bad_gold:
        print(f"  Skipped {result.skipped_bad_gold} cases (gold SQL did not execute).")
    if result.skipped_malformed:
        print(f"  Skipped {result.skipped_malformed} malformed records.")
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
