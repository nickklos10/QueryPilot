"""``querypilot proxy``: run the Postgres wire-protocol proxy from the command line."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from querypilot.access import AccessPolicy
from querypilot.audit import JSONLAuditSink
from querypilot.core.config import QueryPilotConfig
from querypilot.project import ProjectConfig, find_config, load_project_config


def run_proxy(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        from querypilot.proxy.server import QueryPilotProxy
    except ImportError as exc:
        raise SystemExit(f'{exc}\nInstall the proxy extra: pip install "querypilot[proxy]"') from exc

    project, policy_path = _load_policy(args.policy)
    upstream = args.upstream or project.database_url
    if not upstream:
        raise SystemExit("No upstream database: pass --upstream postgresql://... or set database_url in the policy file.")
    if not upstream.split("://", 1)[0].startswith(("postgresql", "postgres")):
        raise SystemExit("The proxy speaks the Postgres protocol; --upstream must be a postgresql:// URL.")
    host, port = _listen_address(args.listen)

    config = QueryPilotConfig(
        dialect="postgres",
        max_rows=args.max_rows or project.max_rows,
        timeout_seconds=args.timeout_seconds or project.timeout_seconds,
        allowed_tables=project.allowed_tables,
        blocked_tables=project.blocked_tables,
        schemas=project.schemas,
        sample_values=0,
        access_policy=project.access_policy or AccessPolicy(),
    )
    try:
        proxy = QueryPilotProxy(
            upstream,
            config=config,
            host=host,
            port=port,
            password=args.password,
            audit_sink=JSONLAuditSink(args.audit_log, max_cached=1000),
            allow_catalog=not args.no_catalog,
        )
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Could not start the proxy: {exc}") from exc

    bound_host, bound_port = proxy.address
    policy = f"policy {policy_path}" if policy_path else "default policy (read-only, no row filters or masks)"
    print(
        f"QueryPilot proxy on postgresql://{bound_host}:{bound_port} -> {_redact(upstream)}\n"
        f"  {policy}; max_rows={config.max_rows}; audit log {Path(args.audit_log).resolve()}\n"
        f"  connect any Postgres client, e.g. psql \"postgresql://agent@{bound_host}:{bound_port}/postgres\"",
        file=sys.stderr,
    )
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.close()
    return 0


def _load_policy(path: str | None) -> tuple[ProjectConfig, Path | None]:
    policy_path = Path(path) if path else find_config()
    if policy_path is None:
        return ProjectConfig(), None
    if not policy_path.is_file():
        raise SystemExit(f"Policy file not found: {policy_path}")
    try:
        return load_project_config(policy_path), policy_path
    except (ValueError, ImportError) as exc:
        raise SystemExit(f"Invalid policy file {policy_path}: {exc}") from exc


def _listen_address(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise SystemExit(f"--listen must be HOST:PORT, got {value!r}")
    return host.strip("[]") or "127.0.0.1", int(port)


def _redact(url: str) -> str:
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - only used for display
        return url.split("@")[-1]
