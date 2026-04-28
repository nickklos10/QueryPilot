from __future__ import annotations

import argparse
import json
import os

from querypilot.access import AccessPolicy
from querypilot import QueryPilot


def main(argv: list[str] | None = None) -> None:
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

    args = parser.parse_args(argv)
    if args.command == "serve":
        _serve(args)
    elif args.command == "mcp":
        _mcp(args)


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


def _access_policy_from_args(args: argparse.Namespace) -> AccessPolicy | None:
    if not args.access_policy_json:
        return None
    try:
        payload = json.loads(args.access_policy_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --access-policy-json: {exc}") from exc
    return AccessPolicy.model_validate(payload)


if __name__ == "__main__":
    main()
