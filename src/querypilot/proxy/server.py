"""A Postgres wire-protocol proxy that enforces QueryPilot policies on every statement.

Clients (psql, drivers, ORMs, MCP database servers, BI tools) connect to the proxy
as if it were Postgres. The proxy terminates the client protocol with ``pygwire``,
routes each statement through ``StatementPolicy`` and runs what survives on its
own upstream connection, inside a READ ONLY transaction. Result rows are masked by
position before they go back, and every statement is written to the audit sink.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Self
from uuid import uuid4

from pygwire import ConnectionPhase, FrontendMessageDecoder, PygwireError
from pygwire.constants import StartupRequestCode, TransactionStatus
from pygwire.messages import (
    AuthenticationCleartextPassword,
    AuthenticationOk,
    BackendKeyData,
    Bind,
    BindComplete,
    CancelRequest,
    Close,
    CloseComplete,
    CommandComplete,
    DataRow,
    Describe,
    EmptyQueryResponse,
    ErrorResponse,
    Execute,
    FieldDescription,
    Flush,
    GSSEncRequest,
    NegotiateProtocolVersion,
    NoData,
    ParameterDescription,
    ParameterStatus,
    Parse,
    ParseComplete,
    PasswordMessage,
    PortalSuspended,
    Query,
    ReadyForQuery,
    RowDescription,
    SSLRequest,
    StartupMessage,
    Sync,
    Terminate,
)

from querypilot.access.policy import MASK_STRENGTH
from querypilot.audit import (
    AuditMetadata,
    AuditSink,
    InMemoryAuditSink,
    QueryAuditRecord,
)
from querypilot.core.config import QueryPilotConfig
from querypilot.proxy.statements import (
    SETTABLE,
    Statement,
    StatementPolicy,
    split_statements,
)
from querypilot.proxy.upstream import (
    REPORTED_PARAMETERS,
    Column,
    Result,
    Upstream,
    UpstreamError,
)
from querypilot.schema.pruning import visible_schema
from querypilot.validation.feedback import redact_values, repair_hints

logger = logging.getLogger("querypilot.proxy")

_TEXT_OID = 25
_UTF8_NAMES = {"utf8", "utf-8", "unicode"}


class QueryPilotProxy:
    """Listen for Postgres clients and forward their policy-checked SQL to ``upstream_url``.

    ``config`` carries the same policies as the SDK (access policy, allowed and
    blocked tables, ``max_rows``, ``timeout_seconds``, ``schemas``). Listening on
    anything but a loopback address requires ``password``.
    """

    def __init__(
        self,
        upstream_url: str,
        *,
        config: QueryPilotConfig | None = None,
        host: str = "127.0.0.1",
        port: int = 6543,
        password: str | None = None,
        audit_sink: AuditSink | None = None,
        allow_catalog: bool = True,
    ) -> None:
        if password is None and not _is_loopback(host):
            raise ValueError("Set a proxy password to listen on a non-loopback address.")
        self.config = (config or QueryPilotConfig(sample_values=0)).model_copy(update={"dialect": "postgres"})
        self.conninfo = _libpq_url(upstream_url)
        self.password = password
        self.audit_sink = audit_sink or InMemoryAuditSink()
        self._audit_lock = threading.Lock()
        self._sessions: dict[tuple[int, bytes], _Session] = {}
        self._sessions_lock = threading.Lock()

        schema = _introspect(upstream_url, self.config)
        catalog_relations, current_schema = _catalog_info(self.conninfo, self.config.timeout_seconds)
        self.policy = StatementPolicy(
            self.config,
            schema,
            catalog_relations=catalog_relations,
            data_schemas=set(self.config.schemas or [current_schema]),
            allow_catalog=allow_catalog,
        )
        self.visible_schema = visible_schema(schema, self.config)

        self._server = _TCPServer((host, port), _Handler)
        self._server.proxy = self
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return host, port

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def start(self) -> QueryPilotProxy:
        """Serve in a background thread; returns ``self``."""
        self._thread = threading.Thread(target=self.serve_forever, name="querypilot-proxy", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        with self._sessions_lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.shutdown()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()

    def write_audit(self, record: QueryAuditRecord) -> None:
        with self._audit_lock:
            self.audit_sink.write(record)

    def register(self, session: _Session) -> tuple[int, bytes]:
        with self._sessions_lock:
            while True:
                key = (secrets.randbelow(2**31 - 1) + 1, secrets.token_bytes(4))
                if key not in self._sessions:
                    self._sessions[key] = session
                    return key

    def unregister(self, key: tuple[int, bytes] | None) -> None:
        with self._sessions_lock:
            self._sessions.pop(key, None)

    def cancel(self, process_id: int, secret_key: bytes) -> None:
        with self._sessions_lock:
            session = self._sessions.get((process_id, secret_key))
        if session is not None and session.upstream is not None:
            session.upstream.cancel()


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    proxy: QueryPilotProxy


class _Handler(socketserver.BaseRequestHandler):
    server: _TCPServer

    def handle(self) -> None:
        _Session(self.server.proxy, self.request, self.client_address).run()


class _ClientError(Exception):
    def __init__(self, sqlstate: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.message = message
        self.hint = hint


@dataclass
class _Prepared:
    statement: Statement
    param_types: list[int]
    described: Result | None = None


@dataclass
class _Portal:
    prepared: _Prepared
    params: list[bytes | None]
    param_formats: list[int]
    result_format: int
    rows: list[list[bytes | None]] | None = None
    position: int = 0
    tag: str = ""
    masks: list[str | None] = field(default_factory=list)


class _Session:
    def __init__(self, proxy: QueryPilotProxy, sock: socket.socket, address) -> None:
        self.proxy = proxy
        self.policy = proxy.policy
        self.sock = sock
        self.address = address
        self.decoder = FrontendMessageDecoder()
        self.upstream: Upstream | None = None
        self.key: tuple[int, bytes] | None = None
        self.metadata = AuditMetadata(session_id=str(uuid4()))
        self.statements: dict[str, _Prepared] = {}
        self.portals: dict[str, _Portal] = {}
        # Client-visible transaction state: "idle", "open" or "failed".
        self.txn = "idle"
        # An upstream READ ONLY transaction the proxy opened for an extended batch.
        self.implicit = False
        self.implicit_failed = False
        self.skip_until_sync = False
        self.reported: dict[str, str | None] = {}

    # Connection lifecycle

    def run(self) -> None:
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._startup():
                self._serve()
        except (ConnectionError, OSError, PygwireError) as exc:
            logger.debug("client %s disconnected: %s", self.address, exc)
        finally:
            self.proxy.unregister(self.key)
            if self.upstream is not None:
                self.upstream.close()
            try:
                self.sock.close()
            except OSError:
                pass

    def shutdown(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _messages(self) -> Iterator:
        while True:
            # Parse lazily so the decoder phase can change between messages.
            yield from self.decoder
            data = self.sock.recv(65536)
            if not data:
                return
            self.decoder.feed(data)

    def _send(self, *messages) -> None:
        self.sock.sendall(b"".join(message.to_wire() for message in messages))

    def _fatal(self, sqlstate: str, message: str) -> None:
        self._send(ErrorResponse(fields={"S": "FATAL", "V": "FATAL", "C": sqlstate, "M": message}))

    def _startup(self) -> bool:
        self.decoder.phase = ConnectionPhase.STARTUP
        messages = self._messages()
        for message in messages:
            if isinstance(message, (SSLRequest, GSSEncRequest)):
                # No TLS termination yet: clients that prefer TLS fall back to plain TCP.
                self.sock.sendall(b"N")
                continue
            if isinstance(message, CancelRequest):
                self.proxy.cancel(message.process_id, message.secret_key)
                return False
            if isinstance(message, StartupMessage):
                return self._accept(message, messages)
            self._fatal("08P01", "Unexpected message during startup.")
            return False
        return False

    def _accept(self, startup: StartupMessage, messages: Iterator) -> bool:
        params = dict(startup.params)
        if "replication" in params:
            self._fatal("0A000", "Replication connections are not supported by the QueryPilot proxy.")
            return False
        encoding = params.get("client_encoding", "UTF8").strip("'\"").lower()
        if encoding not in _UTF8_NAMES:
            self._fatal("0A000", "The QueryPilot proxy only supports client_encoding UTF8.")
            return False
        unrecognized = [name for name in params if name.startswith("_pq_.")]
        if startup.protocol_version != StartupRequestCode.V3_0 or unrecognized:
            self._send(NegotiateProtocolVersion(newest_minor=0, unrecognized=unrecognized))

        user = params.get("user", "")
        self.metadata.actor = user or None
        self.metadata.app_name = params.get("application_name") or None
        if self.proxy.password is not None:
            self._send(AuthenticationCleartextPassword())
            self.decoder.phase = ConnectionPhase.AUTHENTICATING
            message = next(messages, None)
            password = message.password if isinstance(message, PasswordMessage) else None
            if isinstance(password, str):
                password = password.encode()
            if password is None or not hmac.compare_digest(password, self.proxy.password.encode()):
                self._fatal("28P01", f'password authentication failed for user "{user}"')
                return False

        try:
            self.upstream = Upstream(self.proxy.conninfo, timeout_seconds=self.proxy.config.timeout_seconds)
        except UpstreamError as exc:
            logger.error("upstream connection failed: %s", exc)
            self._fatal("08006", "The QueryPilot proxy could not connect to the upstream database.")
            return False
        for name, value in params.items():
            # Formatting settings drivers pass at startup (DateStyle, TimeZone, ...)
            # take the same allowlisted SET path as a client SET statement.
            if name.lower() in SETTABLE and name.lower() != "client_encoding":
                statement = self.policy.classify(f"SET {name} TO '{value.replace(chr(39), chr(39) * 2)}'")
                if statement.kind == "set":
                    try:
                        self.upstream.execute(statement.upstream_sql)
                    except UpstreamError:
                        logger.info("ignored startup setting %s=%r", name, value)

        self.key = self.proxy.register(self)
        self.reported = self._reported_parameters()
        self._send(
            AuthenticationOk(),
            *(ParameterStatus(name=name, value=value) for name, value in self.reported.items() if value is not None),
            BackendKeyData(process_id=self.key[0], secret_key=self.key[1]),
            ReadyForQuery(status=TransactionStatus.IDLE),
        )
        self.decoder.phase = ConnectionPhase.READY
        logger.info("client %s connected as %r", self.address, user)
        return True

    def _reported_parameters(self) -> dict[str, str | None]:
        reported = {name: self.upstream.parameter(name) for name in REPORTED_PARAMETERS}
        reported["client_encoding"] = "UTF8"
        return reported

    def _serve(self) -> None:
        handlers = {
            Query: self._on_query,
            Parse: self._on_parse,
            Bind: self._on_bind,
            Describe: self._on_describe,
            Execute: self._on_execute,
            Close: self._on_close,
        }
        for message in self._messages():
            if isinstance(message, Terminate):
                return
            if isinstance(message, Sync):
                self._on_sync()
                continue
            if isinstance(message, Flush):
                continue
            handler = handlers.get(type(message))
            if self.skip_until_sync and not isinstance(message, Query):
                continue
            try:
                if handler is None:
                    raise _ClientError("0A000", f"{type(message).__name__} is not supported by the QueryPilot proxy.")
                handler(message)
            except _Handled:
                self.skip_until_sync = True
            except _ClientError as exc:
                self._send_error(exc.sqlstate, exc.message, exc.hint)
                self.skip_until_sync = not isinstance(message, Query)
            except UpstreamError as exc:
                self._upstream_failed()
                self._send_upstream_error(exc, None)
                self.skip_until_sync = not isinstance(message, Query)

    def _status(self) -> TransactionStatus:
        if self.txn == "open":
            return TransactionStatus.IN_TRANSACTION
        if self.txn == "failed":
            return TransactionStatus.ERROR_TRANSACTION
        return TransactionStatus.IDLE

    # Simple query protocol

    def _on_query(self, message: Query) -> None:
        for text in split_statements(message.query_string):
            statement = self.policy.classify(text)
            if statement.kind == "empty":
                self._send(EmptyQueryResponse())
                continue
            if not self._run_simple(statement):
                break
        self._end_implicit()
        self._send(ReadyForQuery(status=self._status()))

    def _run_simple(self, statement: Statement) -> bool:
        started = time.perf_counter()
        try:
            result, tag = self._execute(statement)
        except _ClientError as exc:
            self._audit(statement, error=exc.message)
            self._client_failed()
            self._send_error(exc.sqlstate, exc.message, exc.hint)
            return False
        except UpstreamError as exc:
            self._audit(statement, error=exc.fields.get("M"))
            self._upstream_failed()
            self._end_implicit()
            self._send_upstream_error(exc, statement)
            return False
        if result is not None and result.columns is not None:
            masks = statement.output_masks
            self._send(
                self._row_description(result.columns, masks, 0),
                *(DataRow(columns=_mask_row(row, masks)) for row in result.rows),
            )
        self._audit(statement, executed=True, result=result, started=started)
        self._send(CommandComplete(tag=tag))
        # Each simple-query statement commits on its own.
        self._end_implicit()
        return True

    # Extended query protocol

    def _on_parse(self, message: Parse) -> None:
        if message.statement and message.statement in self.statements:
            raise _ClientError("42P05", f'prepared statement "{message.statement}" already exists')
        statement = self.policy.classify(message.query)
        if statement.kind == "rejected":
            self._audit(statement, error=statement.message)
            self._client_failed()
            raise self._rejection(statement)
        self.statements[message.statement] = _Prepared(statement, list(message.param_types))
        self._send(ParseComplete())

    def _on_bind(self, message: Bind) -> None:
        prepared = self.statements.get(message.statement)
        if prepared is None:
            raise _ClientError("26000", f'prepared statement "{message.statement}" does not exist')
        if message.portal and message.portal in self.portals:
            raise _ClientError("42P03", f'cursor "{message.portal}" already exists')
        formats = set(message.result_formats)
        if len(formats) > 1:
            raise _ClientError("0A000", "Mixed per-column result formats are not supported by the QueryPilot proxy.")
        if self.txn == "failed" and prepared.statement.kind not in ("commit", "rollback", "savepoint"):
            raise _aborted()
        self.portals[message.portal] = _Portal(
            prepared=prepared,
            params=list(message.param_values),
            param_formats=list(message.param_formats),
            result_format=formats.pop() if formats else 0,
            masks=prepared.statement.output_masks,
        )
        self._send(BindComplete())

    def _on_describe(self, message: Describe) -> None:
        if message.kind == "S":
            prepared = self.statements.get(message.name)
            if prepared is None:
                raise _ClientError("26000", f'prepared statement "{message.name}" does not exist')
            described = self._describe(prepared)
            param_types = described.param_types if described is not None else prepared.param_types
            self._send(ParameterDescription(type_oids=param_types))
            self._send_description(described, prepared.statement.output_masks, 0)
            return
        portal = self.portals.get(message.name)
        if portal is None:
            raise _ClientError("34000", f'portal "{message.name}" does not exist')
        self._send_description(self._describe(portal.prepared), portal.masks, portal.result_format)

    def _describe(self, prepared: _Prepared) -> Result | None:
        statement = prepared.statement
        if not statement.returns_rows:
            return None
        if prepared.described is None:
            try:
                prepared.described = self.upstream.describe(statement.upstream_sql, prepared.param_types)
            except UpstreamError as exc:
                self._upstream_failed()
                self._send_upstream_error(exc, statement)
                self.skip_until_sync = True
                raise _Handled from exc
        return prepared.described

    def _send_description(self, described: Result | None, masks: list[str | None], result_format: int) -> None:
        if described is None or described.columns is None:
            self._send(NoData())
        else:
            self._send(self._row_description(described.columns, masks, result_format))

    def _on_execute(self, message: Execute) -> None:
        portal = self.portals.get(message.portal)
        if portal is None:
            raise _ClientError("34000", f'portal "{message.portal}" does not exist')
        statement = portal.prepared.statement
        if statement.kind == "empty":
            self._send(EmptyQueryResponse())
            return
        if portal.rows is None:
            started = time.perf_counter()
            try:
                result, portal.tag = self._execute(
                    statement,
                    params=portal.params,
                    param_types=_param_types(portal.prepared),
                    param_formats=portal.param_formats,
                    result_format=portal.result_format,
                )
            except _ClientError as exc:
                self._audit(statement, error=exc.message)
                self._client_failed()
                raise
            except UpstreamError as exc:
                self._audit(statement, error=exc.fields.get("M"))
                self._upstream_failed()
                self._send_upstream_error(exc, statement)
                self.skip_until_sync = True
                return
            portal.rows = result.rows if result is not None else []
            self._audit(statement, executed=True, result=result, started=started)

        limit = message.max_rows if message.max_rows > 0 else len(portal.rows)
        batch = portal.rows[portal.position : portal.position + limit]
        portal.position += len(batch)
        self._send(*(DataRow(columns=_mask_row(row, portal.masks)) for row in batch))
        if portal.position < len(portal.rows):
            self._send(PortalSuspended())
        else:
            self._send(CommandComplete(tag=portal.tag))

    def _on_close(self, message: Close) -> None:
        if message.kind == "S":
            self.statements.pop(message.name, None)
        else:
            self.portals.pop(message.name, None)
        self._send(CloseComplete())

    def _on_sync(self) -> None:
        self._end_implicit()
        if self.txn == "idle":
            # Portals live until the end of their transaction.
            self.portals.clear()
        self.skip_until_sync = False
        self._send(ReadyForQuery(status=self._status()))

    # Statement execution, shared by both protocols

    def _execute(
        self,
        statement: Statement,
        *,
        params: list[bytes | None] | None = None,
        param_types: list[int] | None = None,
        param_formats: list[int] | None = None,
        result_format: int = 0,
    ) -> tuple[Result | None, str]:
        kind = statement.kind
        if kind == "rejected":
            raise self._rejection(statement)
        if self.txn == "failed" and kind not in ("commit", "rollback", "savepoint"):
            raise _aborted()
        upstream = self.upstream

        if kind == "begin":
            if self.txn == "idle":
                if self.implicit:
                    # BEGIN inside an extended batch turns its implicit transaction explicit.
                    self.implicit = False
                else:
                    upstream.execute(statement.upstream_sql)
                self.txn = "open"
            return None, "BEGIN"
        if kind in ("commit", "rollback"):
            if self.txn == "idle":
                return None, statement.tag
            failed = self.txn == "failed"
            self.txn = "idle"
            upstream.execute("ROLLBACK" if failed else statement.upstream_sql)
            return None, "ROLLBACK" if failed else statement.tag
        if kind == "savepoint":
            if self.txn == "idle" and not self.implicit:
                raise _ClientError("25P01", f"{statement.tag} can only be used in transaction blocks")
            upstream.execute(statement.upstream_sql)
            if statement.tag == "ROLLBACK":
                self.txn = "open"
            return None, statement.tag
        if kind == "set":
            upstream.execute(statement.upstream_sql)
            self._report_changes()
            return None, statement.tag
        if kind == "deallocate":
            if statement.target is None:
                self.statements.clear()
            else:
                self.statements.pop(statement.target, None)
            return None, statement.tag

        if self.txn == "idle" and not self.implicit:
            upstream.execute("BEGIN READ ONLY")
            self.implicit = True
        result = upstream.execute(statement.upstream_sql, params, param_types, param_formats, result_format)
        return result, result.tag

    def _end_implicit(self) -> None:
        if not self.implicit:
            return
        self.implicit = False
        failed, self.implicit_failed = self.implicit_failed, False
        try:
            self.upstream.execute("ROLLBACK" if failed else "COMMIT")
        except UpstreamError as exc:
            self._send_upstream_error(exc, None)

    def _client_failed(self) -> None:
        # A rejected statement aborts the client's transaction, as an error would in Postgres.
        if self.txn == "open":
            self.txn = "failed"
        if self.implicit:
            self.implicit_failed = True

    def _upstream_failed(self) -> None:
        self._client_failed()

    def _report_changes(self) -> None:
        current = self._reported_parameters()
        changed = [
            ParameterStatus(name=name, value=value)
            for name, value in current.items()
            if value is not None and self.reported.get(name) != value
        ]
        self.reported = current
        if changed:
            self._send(*changed)

    # Responses

    def _row_description(self, columns: list[Column], masks: list[str | None], result_format: int) -> RowDescription:
        fields = []
        for index, column in enumerate(columns):
            mode = _mask_mode(masks, index, len(columns))
            if mode in ("redact", "hash"):
                # The masked value is text whatever the source type, and its origin is hidden.
                fields.append(
                    FieldDescription(
                        name=column.name, type_oid=_TEXT_OID, type_size=-1, type_modifier=-1, format_code=result_format
                    )
                )
                continue
            fields.append(
                FieldDescription(
                    name=column.name,
                    table_oid=column.table_oid,
                    column_attr=column.column_attr,
                    type_oid=column.type_oid,
                    type_size=column.type_size,
                    type_modifier=column.type_modifier,
                    format_code=result_format,
                )
            )
        return RowDescription(fields=fields)

    def _rejection(self, statement: Statement) -> _ClientError:
        message = statement.message or "Query rejected by policy."
        hints = repair_hints([message], self.proxy.visible_schema, statement.sql) if statement.validation else []
        return _ClientError(
            statement.sqlstate or "42501",
            f"QueryPilot blocked this query: {message}",
            "\n".join(hints) or None,
        )

    def _send_error(self, sqlstate: str, message: str, hint: str | None = None) -> None:
        fields = {"S": "ERROR", "V": "ERROR", "C": sqlstate, "M": message}
        if hint:
            fields["H"] = hint
        self._send(ErrorResponse(fields=fields))

    def _send_upstream_error(self, exc: UpstreamError, statement: Statement | None) -> None:
        # Database messages can echo stored values; redact them as the SDK does.
        sql = statement.sql if statement is not None else None
        fields = {key: exc.fields[key] for key in ("S", "V", "C") if key in exc.fields}
        fields["M"] = redact_values(exc.fields.get("M", "upstream error"), self.proxy.visible_schema, sql)
        if exc.fields.get("H"):
            fields["H"] = redact_values(exc.fields["H"], self.proxy.visible_schema, sql)
        self._send(ErrorResponse(fields=fields))

    def _audit(
        self,
        statement: Statement,
        *,
        executed: bool = False,
        result: Result | None = None,
        started: float | None = None,
        error: str | None = None,
    ) -> None:
        validation = statement.validation
        record = QueryAuditRecord.create(
            operation=f"proxy_{statement.kind}",
            metadata=self.metadata,
            sql=statement.sql,
            rewritten_sql=statement.upstream_sql,
            validation=validation,
            valid=statement.kind != "rejected",
            executed=executed,
            row_count=len(result.rows) if result is not None else None,
            execution_time_ms=int((time.perf_counter() - started) * 1000) if started is not None else None,
            error="; ".join(statement.errors) if statement.errors else error,
            access_policy=validation.access_policy if validation is not None else {},
        )
        self.proxy.write_audit(record)


class _Handled(_ClientError):
    """An error already sent to the client."""

    def __init__(self) -> None:
        super().__init__("", "")


def _aborted() -> _ClientError:
    return _ClientError("25P02", "current transaction is aborted, commands ignored until end of transaction block")


def _param_types(prepared: _Prepared) -> list[int]:
    if prepared.described is not None:
        return prepared.described.param_types
    return prepared.param_types


def _mask_mode(masks: list[str | None], index: int, width: int) -> str | None:
    if not any(masks):
        return None
    if len(masks) != width:
        # The row shape does not match the analyzed projection: mask everything.
        return min((mode for mode in masks if mode), key=MASK_STRENGTH.__getitem__)
    return masks[index]


def _mask_row(row: list[bytes | None], masks: list[str | None]) -> list[bytes | None]:
    if not any(masks):
        return row
    return [_mask_value(value, _mask_mode(masks, index, len(row))) for index, value in enumerate(row)]


def _mask_value(value: bytes | None, mode: str | None) -> bytes | None:
    if mode is None:
        return value
    if mode == "null":
        return None
    if mode == "hash":
        # Hashes the value as Postgres encoded it; for text results this matches the SDK.
        return hashlib.sha256(value or b"None").hexdigest().encode()
    return b"[REDACTED]"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _libpq_url(url: str) -> str:
    """Drop a SQLAlchemy driver suffix (postgresql+psycopg://) so libpq can parse the URL."""
    scheme, sep, rest = url.partition("://")
    if sep and "+" in scheme:
        scheme = scheme.split("+", 1)[0]
    return f"{scheme}{sep}{rest}"


def _introspect(upstream_url: str, config: QueryPilotConfig):
    from querypilot.connectors.postgres import PostgresConnector

    connector = PostgresConnector(upstream_url, config.timeout_seconds, schemas=config.schemas)
    try:
        return connector.get_schema()
    finally:
        connector.engine.dispose()


def _catalog_info(conninfo: str, timeout_seconds: int) -> tuple[set[str], str]:
    upstream = Upstream(conninfo, timeout_seconds=timeout_seconds)
    try:
        relations = upstream.execute(
            "SELECT relname FROM pg_catalog.pg_class WHERE relnamespace = 'pg_catalog'::regnamespace"
        )
        current = upstream.execute("SELECT current_schema()")
    finally:
        upstream.close()
    names = {row[0].decode() for row in relations.rows if row[0] is not None}
    current_schema = current.rows[0][0].decode() if current.rows and current.rows[0][0] else "public"
    return names, current_schema
