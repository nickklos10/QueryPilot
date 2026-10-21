"""The proxy against a real Postgres.

Uses QUERYPILOT_TEST_POSTGRES_URL when set (a superuser URL; the tests create and
drop their own database), otherwise starts a postgres:16 container with Docker,
otherwise skips. The psql test also needs a ``psql`` binary on PATH (or
QUERYPILOT_TEST_PSQL).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest

pytest.importorskip("pygwire")
psycopg = pytest.importorskip("psycopg")

from psycopg import sql as pgsql
from pygwire import FrontendConnection
from pygwire.messages import (
    Bind,
    CommandComplete,
    DataRow,
    Execute,
    Parse,
    PortalSuspended,
    ReadyForQuery,
    StartupMessage,
    Sync,
)

from querypilot.access import AccessPolicy
from querypilot.audit import InMemoryAuditSink
from querypilot.core.config import QueryPilotConfig
from querypilot.proxy import QueryPilotProxy

MAX_ROWS = 50
SEED = """
CREATE TABLE customers (id int PRIMARY KEY, name text, email text, phone text, tenant_id int);
CREATE TABLE orders (
    id int PRIMARY KEY, customer_id int REFERENCES customers (id), amount numeric(10, 2), tenant_id int
);
INSERT INTO customers VALUES
    (1, 'Ann', 'ann@a.test', '555-0101', 1),
    (2, 'Bob', 'bob@b.test', '555-0102', 2),
    (3, 'Cy', 'cy@a.test', '555-0103', 1);
-- 300 orders: 200 for tenant 1 (customers 1 and 3), 100 for tenant 2 (customer 2).
INSERT INTO orders
SELECT g, 1 + g % 3, g * 1.5, CASE WHEN 1 + g % 3 = 2 THEN 2 ELSE 1 END FROM generate_series(1, 300) g;
"""


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    url = os.getenv("QUERYPILOT_TEST_POSTGRES_URL")
    if url:
        yield url
        return
    docker = shutil.which("docker")
    if docker is None or subprocess.run([docker, "info"], capture_output=True, check=False).returncode != 0:
        pytest.skip("Docker is not available; set QUERYPILOT_TEST_POSTGRES_URL to use an existing Postgres.")
    name = f"querypilot-proxy-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [docker, "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=querypilot",
         "-p", "127.0.0.1::5432", "postgres:16-alpine"],
        check=True,
        capture_output=True,
    )
    try:
        port = subprocess.run(
            [docker, "port", name, "5432/tcp"], check=True, capture_output=True, text=True
        ).stdout.split(":")[-1].strip()
        url = f"postgresql://postgres:querypilot@127.0.0.1:{port}/postgres"
        _wait_for(url)
        yield url
    finally:
        subprocess.run([docker, "stop", name], capture_output=True, check=False)


@pytest.fixture(scope="module")
def upstream_url(server_url: str) -> Iterator[str]:
    """A fresh database with the seed data, dropped afterwards."""
    database = f"qp_proxy_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(server_url, autocommit=True) as admin:
        admin.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(database)))
    url = _with_database(server_url, database)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SEED)
    yield url
    with psycopg.connect(server_url, autocommit=True) as admin:
        admin.execute(pgsql.SQL("DROP DATABASE {} WITH (FORCE)").format(pgsql.Identifier(database)))


@pytest.fixture(scope="module")
def proxy(upstream_url: str) -> Iterator[QueryPilotProxy]:
    config = QueryPilotConfig(
        max_rows=MAX_ROWS,
        sample_values=0,
        access_policy=AccessPolicy(
            row_filters={"orders": "tenant_id = 1", "customers": "tenant_id = 1"},
            masking_rules={"customers": {"email": {"mode": "redact"}, "phone": {"mode": "null"}}},
        ),
    )
    with QueryPilotProxy(upstream_url, config=config, port=0, audit_sink=InMemoryAuditSink()) as running:
        yield running


@pytest.fixture()
def proxy_url(proxy: QueryPilotProxy) -> str:
    host, port = proxy.address
    return f"postgresql://agent@{host}:{port}/app"


def test_psycopg_connects_through_the_proxy(proxy_url: str) -> None:
    with psycopg.connect(proxy_url) as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
        names = conn.execute("SELECT name FROM customers ORDER BY id").fetchall()
        read_only = conn.execute("SELECT current_setting('transaction_read_only')").fetchone()[0]

    assert version.startswith("PostgreSQL")
    assert names == [("Ann",), ("Cy",)]
    assert read_only == "on"


def test_psql_connects_through_the_proxy(proxy_url: str) -> None:
    psql = os.getenv("QUERYPILOT_TEST_PSQL") or shutil.which("psql")
    if psql is None:
        pytest.skip("psql is not installed.")

    def run(command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [psql, proxy_url, "-X", "-tA", "-c", command], capture_output=True, text=True, timeout=30, check=False
        )

    query = run("SELECT name, email FROM customers ORDER BY id")
    tables = run("\\dt")
    write = run("DELETE FROM orders")

    assert query.returncode == 0, query.stderr
    assert query.stdout.split() == ["Ann|[REDACTED]", "Cy|[REDACTED]"]
    assert "orders" in tables.stdout
    assert write.returncode != 0
    assert "QueryPilot blocked this query" in write.stderr


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM orders",
        "UPDATE orders SET amount = 0",
        "INSERT INTO orders VALUES (999, 1, 1, 1)",
        "DROP TABLE orders",
        "TRUNCATE orders",
        "CREATE TABLE stolen AS SELECT * FROM customers",
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT count(*) FROM d",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        "SET default_transaction_read_only = off",
        "BEGIN READ WRITE",
        "COPY orders TO STDOUT",
        # The bypass of BEGIN READ ONLY wrappers in early MCP Postgres servers.
        "COMMIT; DROP TABLE orders",
    ],
)
def test_writes_are_rejected(proxy_url: str, upstream_url: str, statement: str) -> None:
    with psycopg.connect(proxy_url, autocommit=True) as conn:
        with pytest.raises(psycopg.Error) as caught:
            conn.execute(statement)
        # The session stays usable after a rejection.
        assert conn.execute("SELECT count(*) FROM orders").fetchone() == (200,)

    assert "QueryPilot blocked this query" in str(caught.value)
    assert _orders_count(upstream_url) == 300


def test_multi_statement_simple_query_stops_at_the_rejected_statement(proxy_url: str, upstream_url: str) -> None:
    with psycopg.connect(proxy_url, autocommit=True) as conn, pytest.raises(psycopg.Error):
        # No parameters, so psycopg sends this as one simple-protocol Query message.
        conn.execute("SELECT 1; DELETE FROM orders")

    assert _orders_count(upstream_url) == 300


def test_row_filter_holds_under_join(proxy_url: str) -> None:
    with psycopg.connect(proxy_url) as conn:
        inner = conn.execute(
            "SELECT DISTINCT c.tenant_id, o.tenant_id FROM orders o JOIN customers c ON c.id = o.customer_id"
        ).fetchall()
        joined_count = conn.execute(
            "SELECT count(*) FROM orders o JOIN customers c ON c.id = o.customer_id"
        ).fetchone()
        left = conn.execute(
            "SELECT c.name, count(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
            "GROUP BY c.name ORDER BY c.name"
        ).fetchall()
        bob_orders = conn.execute(
            "SELECT count(*) FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE name = 'Bob')"
        ).fetchone()
        right = conn.execute(
            "SELECT count(*) FROM customers c RIGHT JOIN orders o ON o.customer_id = c.id WHERE c.id IS NULL"
        ).fetchone()

    assert inner == [(1, 1)]
    assert joined_count == (200,)
    assert left == [("Ann", 100), ("Cy", 100)]
    assert bob_orders == (0,)
    assert right == (0,)


def test_masking_applies_by_output_position(proxy_url: str) -> None:
    with psycopg.connect(proxy_url) as conn:
        direct = conn.execute("SELECT name, email, phone FROM customers ORDER BY id").fetchall()
        aliased = conn.execute("SELECT upper(email) AS contact, name FROM customers ORDER BY id").fetchall()
        via_cte = conn.execute(
            "WITH x AS (SELECT email AS e FROM customers) SELECT e FROM x ORDER BY e"
        ).fetchall()
        binary = conn.cursor(binary=True).execute(
            "SELECT c.email, o.amount FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.id = %s",
            (3,),
        ).fetchall()

    assert direct == [("Ann", "[REDACTED]", None), ("Cy", "[REDACTED]", None)]
    assert aliased == [("[REDACTED]", "Ann"), ("[REDACTED]", "Cy")]
    assert via_cte == [("[REDACTED]",), ("[REDACTED]",)]
    assert binary == [("[REDACTED]", pytest.approx(4.5))]


def test_limit_cap_applies(proxy_url: str) -> None:
    with psycopg.connect(proxy_url) as conn:
        unbounded = conn.execute("SELECT id FROM orders").fetchall()
        too_large = conn.execute("SELECT id FROM orders LIMIT 1000").fetchall()
        small = conn.execute("SELECT id FROM orders ORDER BY id LIMIT 5").fetchall()
        parameterized = conn.execute("SELECT id FROM orders WHERE amount > %s", (0,)).fetchall()

    assert len(unbounded) == MAX_ROWS
    assert len(too_large) == MAX_ROWS
    assert small == [(2,), (3,), (5,), (6,), (8,)]
    assert len(parameterized) == MAX_ROWS


def test_rejections_are_actionable_postgres_errors(proxy_url: str) -> None:
    with psycopg.connect(proxy_url) as conn:
        with pytest.raises(psycopg.errors.UndefinedColumn) as caught:
            conn.execute("SELECT nope FROM orders")
        diag = caught.value.diag

    assert diag.sqlstate == "42703"
    assert "Unknown column: nope" in diag.message_primary
    assert "customer_id" in diag.message_hint


def test_transactions_are_read_only_and_recover_from_errors(proxy_url: str) -> None:
    with psycopg.connect(proxy_url) as conn:
        conn.execute("SELECT 1")
        with pytest.raises(psycopg.Error):
            conn.execute("SELECT nope FROM orders")
        with pytest.raises(psycopg.errors.InFailedSqlTransaction):
            conn.execute("SELECT 1")
        conn.rollback()
        with conn.transaction():
            isolation = conn.execute("SHOW transaction isolation level").fetchone()
            read_only = conn.execute("SELECT current_setting('transaction_read_only')").fetchone()

    assert isolation == ("read committed",)
    assert read_only == ("on",)


def test_prepared_statements_and_settings(proxy_url: str) -> None:
    with psycopg.connect(proxy_url, autocommit=True) as conn:
        rows = None
        for _ in range(6):  # psycopg switches to a named prepared statement after 5 runs
            rows = conn.execute(
                "SELECT id FROM orders WHERE id < %s ORDER BY id", (4,), prepare=True
            ).fetchall()
        conn.execute("SET TIME ZONE 'America/Chicago'")
        timezone = conn.info.parameter_status("TimeZone")
        with pytest.raises(psycopg.Error):
            conn.execute("SET search_path = pg_temp, public")

    assert rows == [(2,), (3,)]
    assert timezone == "America/Chicago"


def test_catalog_introspection_works_but_cannot_read_data(proxy_url: str) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine(proxy_url.replace("postgresql://", "postgresql+psycopg://"))
    try:
        inspector = sqlalchemy.inspect(engine)
        tables = inspector.get_table_names()
        columns = [column["name"] for column in inspector.get_columns("orders")]
        foreign_keys = inspector.get_foreign_keys("orders")
    finally:
        engine.dispose()

    assert sorted(tables) == ["customers", "orders"]
    assert columns == ["id", "customer_id", "amount", "tenant_id"]
    assert foreign_keys[0]["referred_table"] == "customers"
    with psycopg.connect(proxy_url, autocommit=True) as conn:
        for statement in (
            "SELECT most_common_vals FROM pg_stats",
            "SELECT query_to_xml('SELECT email FROM customers', true, true, '')",
            "SELECT relname, (SELECT email FROM customers LIMIT 1) FROM pg_class",
        ):
            with pytest.raises(psycopg.Error):
                conn.execute(statement)


def test_portal_suspension_with_max_rows(proxy: QueryPilotProxy) -> None:
    """Drive the extended protocol by hand: Execute with max_rows returns PortalSuspended."""
    host, port = proxy.address
    client = FrontendConnection()
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(client.send(StartupMessage(params={"user": "agent", "database": "app"})))
        _read_until(sock, client, ReadyForQuery)
        sock.sendall(
            b"".join(
                client.send(message)
                for message in (
                    Parse(query="SELECT id FROM orders WHERE id <= $1 ORDER BY id", param_types=[23]),
                    Bind(param_values=[b"6"]),
                    Execute(max_rows=2),
                    Execute(max_rows=2),
                    Execute(max_rows=2),
                    Sync(),
                )
            )
        )
        messages = _read_until(sock, client, ReadyForQuery)

    kinds = [type(message).__name__ for message in messages if not isinstance(message, DataRow)]
    rows = [message.columns[0] for message in messages if isinstance(message, DataRow)]
    assert rows == [b"2", b"3", b"5", b"6"]
    assert kinds.count("PortalSuspended") == 1
    assert any(isinstance(message, CommandComplete) for message in messages)
    assert not any(isinstance(message, PortalSuspended) for message in messages[-2:])


def test_password_is_required_when_configured(upstream_url: str) -> None:
    with QueryPilotProxy(upstream_url, port=0, password="s3cret") as locked:
        host, port = locked.address
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(f"postgresql://agent:wrong@{host}:{port}/app").close()
        with psycopg.connect(f"postgresql://agent:s3cret@{host}:{port}/app") as conn:
            assert conn.execute("SELECT count(*) FROM orders").fetchone() == (300,)


def test_every_statement_is_audited(proxy: QueryPilotProxy, proxy_url: str) -> None:
    marker = uuid.uuid4().hex
    with psycopg.connect(proxy_url, autocommit=True) as conn:
        conn.execute(f"SELECT id AS m_{marker} FROM orders LIMIT 1")
        with pytest.raises(psycopg.Error):
            conn.execute(f"DELETE FROM orders WHERE id = 0 AND '{marker}' = ''")
        conn.execute("SET application_name = 'audit-test'")

    records = [record for record in proxy.audit_sink.records if marker in (record.sql or "")]
    executed, rejected = records
    assert executed.operation == "proxy_query"
    assert executed.executed and executed.row_count == 1
    assert "LIMIT 1" in executed.rewritten_sql
    assert executed.actor == "agent" and executed.session_id
    assert rejected.operation == "proxy_rejected"
    assert not rejected.valid and not rejected.executed
    assert any(record.operation == "proxy_set" for record in proxy.audit_sink.records)


def _read_until(sock: socket.socket, client: FrontendConnection, last: type) -> list:
    messages: list = []
    while True:
        data = sock.recv(65536)
        assert data, "proxy closed the connection"
        for message in client.receive(data):
            messages.append(message)
            if isinstance(message, last):
                return messages


def _orders_count(url: str) -> int:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT count(*) FROM orders").fetchone()[0]


def _with_database(url: str, database: str) -> str:
    base, _, _ = url.rpartition("/")
    query = url.partition("?")[2]
    return f"{base}/{database}" + (f"?{query}" if query else "")


def _wait_for(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            psycopg.connect(url, connect_timeout=2).close()
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
