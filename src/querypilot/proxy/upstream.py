"""The proxy's connection to the real database, at the libpq level.

Working with raw ``PGresult`` values (instead of Python objects) keeps the proxy
byte-for-byte faithful: column types, type modifiers and the text or binary
encoding of each value reach the client exactly as Postgres produced them.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg import pq

# Settings Postgres reports to clients with ParameterStatus.
REPORTED_PARAMETERS = (
    "application_name",
    "client_encoding",
    "DateStyle",
    "default_transaction_read_only",
    "in_hot_standby",
    "integer_datetimes",
    "IntervalStyle",
    "is_superuser",
    "server_encoding",
    "server_version",
    "session_authorization",
    "standard_conforming_strings",
    "TimeZone",
)


@dataclass
class Column:
    name: str
    table_oid: int
    column_attr: int
    type_oid: int
    type_size: int
    type_modifier: int


@dataclass
class Result:
    columns: list[Column] | None
    rows: list[list[bytes | None]]
    tag: str
    param_types: list[int]


class UpstreamError(Exception):
    """An error reported by the upstream database."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__(fields.get("M", "upstream error"))
        self.fields = fields


_ERROR_FIELDS = {
    "S": pq.DiagnosticField.SEVERITY,
    "V": pq.DiagnosticField.SEVERITY_NONLOCALIZED,
    "C": pq.DiagnosticField.SQLSTATE,
    "M": pq.DiagnosticField.MESSAGE_PRIMARY,
    "H": pq.DiagnosticField.MESSAGE_HINT,
}


class Upstream:
    """One upstream session. Not thread-safe, except for ``cancel``."""

    def __init__(self, conninfo: str, *, timeout_seconds: int) -> None:
        self.pgconn = pq.PGconn.connect(conninfo.encode())
        if self.pgconn.status != pq.ConnStatus.OK:
            message = self.pgconn.error_message.decode(errors="replace")
            self.pgconn.finish()
            raise UpstreamError({"S": "FATAL", "C": "08001", "M": f"Could not connect upstream: {message}".strip()})
        self._cancel = self.pgconn.get_cancel()
        # Belt and braces: every statement also runs in an explicit READ ONLY
        # transaction, and the client cannot change either setting.
        self.execute("SET default_transaction_read_only = on")
        self.execute(f"SET statement_timeout = {int(timeout_seconds) * 1000}")
        self.execute("SET application_name = 'querypilot-proxy'")

    @property
    def in_transaction(self) -> bool:
        return self.pgconn.transaction_status != pq.TransactionStatus.IDLE

    def parameter(self, name: str) -> str | None:
        value = self.pgconn.parameter_status(name.encode())
        return value.decode() if value is not None else None

    def execute(
        self,
        sql: str,
        params: list[bytes | None] | None = None,
        param_types: list[int] | None = None,
        param_formats: list[int] | None = None,
        result_format: int = 0,
    ) -> Result:
        # Always the extended protocol: Postgres then refuses a second statement, so
        # forwarded text can never smuggle one past the classifier.
        res = self.pgconn.exec_params(
            sql.encode(), params or [], param_types or None, param_formats or None, result_format
        )
        return _result(res)

    def describe(self, sql: str, param_types: list[int]) -> Result:
        """Prepare ``sql`` as the unnamed statement and describe its parameters and columns."""
        _result(self.pgconn.prepare(b"", sql.encode(), param_types or None))
        return _result(self.pgconn.describe_prepared(b""))

    def cancel(self) -> None:
        self._cancel.cancel()

    def close(self) -> None:
        self.pgconn.finish()


def _result(res: pq.PGresult) -> Result:
    if res.status in (pq.ExecStatus.FATAL_ERROR, pq.ExecStatus.NONFATAL_ERROR, pq.ExecStatus.BAD_RESPONSE):
        fields = {}
        for code, field in _ERROR_FIELDS.items():
            value = res.error_field(field)
            if value:
                fields[code] = value.decode(errors="replace")
        if "M" not in fields:
            fields["M"] = res.error_message.decode(errors="replace").strip() or "upstream error"
        fields.setdefault("S", "ERROR")
        fields.setdefault("C", "XX000")
        raise UpstreamError(fields)
    if res.status not in (pq.ExecStatus.COMMAND_OK, pq.ExecStatus.TUPLES_OK, pq.ExecStatus.EMPTY_QUERY):
        raise UpstreamError({"S": "ERROR", "C": "0A000", "M": f"Unsupported upstream result: {res.status.name}"})

    nfields = res.nfields
    columns = None
    if res.status == pq.ExecStatus.TUPLES_OK or nfields:
        columns = [
            Column(
                name=(res.fname(index) or b"").decode(),
                table_oid=res.ftable(index),
                column_attr=res.ftablecol(index),
                type_oid=res.ftype(index),
                type_size=res.fsize(index),
                type_modifier=res.fmod(index),
            )
            for index in range(nfields)
        ]
    rows = [[res.get_value(row, col) for col in range(nfields)] for row in range(res.ntuples)]
    tag = (res.command_status or b"").decode()
    param_types = [res.param_type(index) for index in range(res.nparams)]
    return Result(columns=columns, rows=rows, tag=tag, param_types=param_types)
