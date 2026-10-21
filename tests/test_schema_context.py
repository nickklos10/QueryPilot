"""Schema context: sampling under the access policy, pruning, search and prompt rendering."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from querypilot import QueryPilot
from querypilot.access import AccessPolicy, MaskingRule
from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import ColumnSchema, DatabaseSchema, ForeignKey, TableSchema
from querypilot.generation.prompt_builder import (
    build_schema_context,
    build_sql_generation_prompt,
)
from querypilot.schema.pruning import (
    prune_schema,
    schema_tokens,
    shortest_path,
    visible_schema,
)
from querypilot.schema.sampling import (
    build_sample_query,
    clear_sample_cache,
    column_can_be_sampled,
)
from querypilot.schema.search import search_schema


@pytest.fixture()
def shop_db_url(tmp_path: Path) -> str:
    db_path = tmp_path / "shop.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            customer_name TEXT NOT NULL,
            segment TEXT NOT NULL,
            region TEXT NOT NULL,
            tier TEXT NOT NULL,
            email TEXT NOT NULL,
            external_id TEXT NOT NULL,
            notes TEXT
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            status TEXT NOT NULL,
            amount INTEGER NOT NULL
        );
        CREATE TABLE payroll (id INTEGER PRIMARY KEY, employee TEXT, grade TEXT);
        CREATE TABLE tickets (id INTEGER PRIMARY KEY, priority TEXT, tenant TEXT);

        INSERT INTO customers VALUES
            (1, 'Acme Corp', 'Enterprise', 'EMEA', 'gold', 'a@acme.test', 'X-1', NULL),
            (2, 'Globex', 'SMB', 'NA', 'silver', 'b@globex.test', 'X-2', 'call back'),
            (3, 'Initech', 'SMB', 'NA', 'secret-tier', 'c@initech.test', 'X-3', NULL);
        INSERT INTO orders VALUES (1, 1, 'paid', 100), (2, 2, 'refunded', 50), (3, 2, 'paid', 70);
        INSERT INTO payroll VALUES (1, 'Pat', 'SECRET-GRADE');
        INSERT INTO tickets VALUES (1, 'urgent', 'tenant-a'), (2, 'low', 'tenant-b');
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db_path}"


class CountingConnector:
    """Wraps a connector and counts execute_readonly calls."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[str] = []

    def get_schema(self):
        return self.inner.get_schema()

    def execute_readonly(self, sql: str):
        self.calls.append(sql)
        return self.inner.execute_readonly(sql)


def _policy_qp(url: str) -> QueryPilot:
    return QueryPilot.connect(
        url,
        blocked_tables=["payroll"],
        access_policy=AccessPolicy(
            blocked_columns={"customers": ["region"]},
            masking_rules={"customers": {"tier": MaskingRule(mode="redact")}},
            row_filters={"tickets": "tenant = 'tenant-a'"},
        ),
        hints=["Revenue means SUM(orders.amount) over paid orders."],
        descriptions={"customers": "Companies that buy from us", "orders.status": "Payment state"},
    )


def test_sampling_fills_safe_text_columns_only(shop_db_url: str) -> None:
    qp = _policy_qp(shop_db_url)

    customers = qp.table_details(["customers"])[0]
    samples = {column.name: column.sample_values for column in customers.columns}

    assert samples["segment"] == ["SMB", "Enterprise"]  # most frequent first
    assert samples["customer_name"] == []  # names are personal data or free text
    assert samples["notes"] == []  # free text
    assert samples["tier"] == []  # masked
    assert samples["email"] == []  # sensitive name
    assert samples["external_id"] == []  # ID-like
    assert samples["id"] == []  # primary key
    assert "region" not in samples  # blocked column is hidden entirely


def test_blocked_and_row_filtered_tables_are_never_sampled(shop_db_url: str) -> None:
    qp = _policy_qp(shop_db_url)
    clear_sample_cache(qp.connector)
    counting = CountingConnector(qp.connector)
    qp.connector = counting

    assert qp.table_details(["payroll"]) == []
    tickets = qp.table_details(["tickets"])[0]

    assert all(not column.sample_values for column in tickets.columns)
    assert not any("payroll" in sql or "tickets" in sql for sql in counting.calls)


def test_prompt_never_contains_restricted_values(shop_db_url: str) -> None:
    qp = _policy_qp(shop_db_url)

    schema = qp.schema_for_prompt("Revenue by customer segment")
    prompt = build_sql_generation_prompt("Revenue by customer segment", schema, 10).user_prompt

    assert "'Enterprise'" in prompt
    for leaked in ("payroll", "SECRET-GRADE", "region", "EMEA", "secret-tier", "acme.test", "tenant-a"):
        assert leaked not in prompt
    assert "Revenue means SUM(orders.amount) over paid orders." in prompt


def test_samples_are_cached_per_connector(shop_db_url: str) -> None:
    qp = _policy_qp(shop_db_url)
    clear_sample_cache(qp.connector)
    counting = CountingConnector(qp.connector)
    qp.connector = counting

    qp.schema_for_prompt("a")
    first = len(counting.calls)
    qp.schema_for_prompt("b")
    qp.table_details(["customers", "orders"])

    assert first > 0
    assert len(counting.calls) == first


def test_sampling_errors_are_swallowed(shop_db_url: str) -> None:
    qp = QueryPilot.connect(shop_db_url)

    class Failing(CountingConnector):
        def execute_readonly(self, sql: str):
            raise RuntimeError("boom")

    qp.connector = Failing(qp.connector)
    customers = qp.table_details(["customers"])[0]

    assert all(not column.sample_values for column in customers.columns)


def test_sampling_can_be_disabled(shop_db_url: str) -> None:
    qp = QueryPilot.connect(shop_db_url, sample_values=0)
    counting = CountingConnector(qp.connector)
    qp.connector = counting

    qp.schema_for_prompt("customers")

    assert counting.calls == []


def test_long_values_are_truncated(tmp_path: Path) -> None:
    db = tmp_path / "long.db"
    sqlite3.connect(db).executescript(
        "CREATE TABLE docs (id INTEGER PRIMARY KEY, category TEXT);"
        f"INSERT INTO docs VALUES (1, '{'x' * 200}'), (2, '{'x' * 200}');"
    )
    qp = QueryPilot.connect(f"sqlite:///{db}")

    value = qp.table_details(["docs"])[0].get_column("category").sample_values[0]

    assert len(value) <= 40 and value.endswith("...")


def test_table_details_returns_copies(shop_db_url: str) -> None:
    qp = QueryPilot.connect(shop_db_url)

    qp.table_details(["customers"])[0].columns.clear()

    assert qp.get_schema().get_table("customers").columns
    assert all(not c.sample_values for c in qp.get_schema().get_table("customers").columns)


def test_sample_query_is_bounded_and_quoted() -> None:
    table = TableSchema(name="Order Items", columns=[ColumnSchema(name="Status", type="VARCHAR(20)")])

    sql = build_sample_query(table, table.columns[0], "postgres", 3)

    assert '"Order Items"' in sql and '"Status"' in sql
    assert "LIMIT 10000" in sql and sql.endswith("LIMIT 3")


def test_column_sampling_rules() -> None:
    config = QueryPilotConfig()
    table = TableSchema(
        name="t",
        columns=[],
        foreign_keys=[ForeignKey(columns=["owner"], referred_table="u", referred_columns=["id"])],
    )

    def ok(name: str, type_: str = "TEXT", pk: bool = False) -> bool:
        return column_can_be_sampled(table, ColumnSchema(name=name, type=type_, primary_key=pk), config)

    assert ok("status") and ok("country", "VARCHAR(2)")
    assert not ok("amount", "INTEGER")
    assert not ok("payload", "JSON")
    assert not ok("code", pk=True)
    assert not ok("userId") and not ok("account_uuid") and not ok("owner")
    assert not ok("password_hash") and not ok("api_key") and not ok("phone_number")


@pytest.mark.parametrize(
    "name",
    [
        "name", "customer_name", "companyName", "company", "title", "notes", "comment",
        "description", "billing_address", "ip", "ip_address", "website_url", "ssn",
        "dob", "birth_date", "salary", "user_agent", "message_body",
    ],
)
def test_personal_and_free_text_columns_are_not_sampled(name: str) -> None:
    table = TableSchema(name="t", columns=[])

    assert not column_can_be_sampled(table, ColumnSchema(name=name, type="TEXT"), QueryPilotConfig())


@pytest.mark.parametrize(
    "name", ["status", "segment", "plan", "type", "event_type", "currency", "country", "category", "priority"]
)
def test_category_like_columns_are_sampled(name: str) -> None:
    table = TableSchema(name="t", columns=[])

    assert column_can_be_sampled(table, ColumnSchema(name=name, type="TEXT"), QueryPilotConfig())


def test_only_low_cardinality_columns_keep_samples(tmp_path: Path) -> None:
    db = tmp_path / "cardinality.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT, code TEXT, label TEXT)")
    conn.executemany(
        "INSERT INTO events (kind, code, label) VALUES (?, ?, ?)",
        # kind: 3 repeating values; code: 60 distinct values; label: unique per row.
        [(f"k{i % 3}", f"c{i % 60}", f"row {i}") for i in range(120)],
    )
    conn.execute("CREATE TABLE tiny (id INTEGER PRIMARY KEY, label TEXT)")
    conn.executemany("INSERT INTO tiny (label) VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()
    qp = QueryPilot.connect(f"sqlite:///{db}")

    events = qp.table_details(["events"])[0]
    tiny = qp.table_details(["tiny"])[0]

    assert events.get_column("kind").sample_values == ["k0", "k1", "k2"]
    assert events.get_column("code").sample_values == []  # more than 50 distinct values
    assert events.get_column("label").sample_values == []  # every value is unique
    assert tiny.get_column("label").sample_values == []


# --------------------------------------------------------------------------- pruning


def _chain_schema(extra_tables: int = 0) -> DatabaseSchema:
    """customers <- orders <- order_items -> products, plus filler tables."""

    def cols(*names: str) -> list[ColumnSchema]:
        return [ColumnSchema(name=n, type="INTEGER", primary_key=n == "id") for n in names]

    def fk(column: str, table: str) -> ForeignKey:
        return ForeignKey(columns=[column], referred_table=table, referred_columns=["id"])

    tables = [
        TableSchema(name="customers", columns=cols("id", "customer_name", "segment")),
        TableSchema(name="orders", columns=cols("id", "customer_id", "ordered_at"), foreign_keys=[fk("customer_id", "customers")]),
        TableSchema(
            name="order_items",
            columns=cols("id", "order_id", "product_id", "quantity"),
            foreign_keys=[fk("order_id", "orders"), fk("product_id", "products")],
        ),
        TableSchema(name="products", columns=cols("id", "product_name", "category")),
    ]
    for index in range(extra_tables):
        tables.append(
            TableSchema(
                name=f"audit_log_{index:02d}",
                columns=cols("id", *(f"field_{n}" for n in range(12))),
            )
        )
    return DatabaseSchema(dialect="sqlite", tables=tables)


def test_pruning_is_a_noop_under_budget() -> None:
    schema = _chain_schema(extra_tables=3)

    pruned = prune_schema(schema, "products bought by each customer", schema_tokens(schema) + 10)

    assert pruned is schema


def test_pruning_keeps_fk_bridge_tables() -> None:
    schema = _chain_schema(extra_tables=40)
    # Room for exactly the two matched tables: the bridge must come from FK closure.
    budget = schema_tokens(
        schema.model_copy(update={"tables": [schema.get_table("customers"), schema.get_table("products")]})
    )

    pruned = prune_schema(schema, "which products did each customer buy", budget)
    names = [table.name for table in pruned.tables]

    assert {"customers", "products", "orders", "order_items"} <= set(names)
    assert len(names) < len(schema.tables)
    # Original schema order is preserved.
    assert names[:4] == ["customers", "orders", "order_items", "products"]


def test_shortest_path_through_fk_graph() -> None:
    from querypilot.schema.pruning import foreign_key_graph

    graph = foreign_key_graph(_chain_schema())

    assert shortest_path(graph, "customers", "products") == [
        "customers",
        "orders",
        "order_items",
        "products",
    ]
    assert shortest_path(graph, "customers", "audit_log_00") == []


def test_pruning_always_keeps_the_best_table() -> None:
    schema = _chain_schema(extra_tables=5)

    pruned = prune_schema(schema, "customer segments", 1)

    assert [table.name for table in pruned.tables] == ["customers"]


def test_schema_for_prompt_prunes_above_budget(shop_db_url: str) -> None:
    qp = QueryPilot.connect(shop_db_url, schema_token_budget=60)

    names = [table.name for table in qp.schema_for_prompt("customer segment").tables]

    assert names[0] == "customers"
    assert "payroll" not in names


def test_visible_schema_drops_hidden_tables_columns_and_their_foreign_keys() -> None:
    schema = _chain_schema()
    config = QueryPilotConfig(
        blocked_tables=["products"],
        access_policy=AccessPolicy(allowed_columns={"orders": ["id", "ordered_at"]}),
    )

    visible = visible_schema(schema, config)

    assert visible.get_table("products") is None
    assert [c.name for c in visible.get_table("orders").columns] == ["id", "ordered_at"]
    assert visible.get_table("orders").foreign_keys == []
    assert [fk.referred_table for fk in visible.get_table("order_items").foreign_keys] == ["orders"]
    # The input schema is untouched.
    assert schema.get_table("products") is not None


# --------------------------------------------------------------------------- search


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("revenue by customer", "customers"),
        ("Which customers spend the most?", "customers"),
        ("order item quantity", "order_items"),
        ("product categories", "products"),
    ],
)
def test_search_matches_singular_plural_and_snake_case(query: str, expected: str) -> None:
    schema = _chain_schema()

    assert search_schema(schema, query)[0].table == expected


def test_search_splits_camel_case_and_uses_comments_and_samples() -> None:
    schema = DatabaseSchema(
        dialect="sqlite",
        tables=[
            TableSchema(name="SalesOrders", columns=[ColumnSchema(name="orderTotal", type="INT")]),
            TableSchema(
                name="accounts",
                comment="Paying companies",
                columns=[ColumnSchema(name="segment", type="TEXT", sample_values=["Enterprise", "SMB"])],
            ),
        ],
    )

    assert search_schema(schema, "sales order total")[0].table == "SalesOrders"
    assert search_schema(schema, "enterprise companies")[0].table == "accounts"


# --------------------------------------------------------------------------- prompt


def test_prompt_renders_keys_comments_samples_and_hints() -> None:
    schema = DatabaseSchema(
        dialect="postgres",
        hints=["Active means status = 'active'."],
        tables=[
            TableSchema(
                name="customers",
                comment="Companies that buy from us",
                columns=[
                    ColumnSchema(name="id", type="INTEGER", primary_key=True),
                    ColumnSchema(name="segment", type="TEXT", comment="Sales tier", sample_values=["SMB", "O'Neil"]),
                ],
            ),
            TableSchema(
                name="orders",
                is_view=True,
                columns=[ColumnSchema(name="customer_id", type="INTEGER")],
                foreign_keys=[ForeignKey(columns=["customer_id"], referred_table="customers", referred_columns=["id"])],
            ),
        ],
    )

    prompt = build_sql_generation_prompt("Active customers", schema, 50)
    context = build_schema_context(schema)

    assert "CREATE TABLE customers ( -- Companies that buy from us" in context
    assert "  id INTEGER PRIMARY KEY," in context
    assert "  segment TEXT -- Sales tier; e.g. 'SMB', 'O''Neil'" in context
    assert "CREATE VIEW orders (" in context
    assert "-- orders.customer_id -> customers.id" in context
    assert "Business rules / hints:\n- Active means status = 'active'." in prompt.user_prompt
    assert "Write a single PostgreSQL SELECT" in prompt.instructions
    assert "Put no prose or markdown in the sql value" in prompt.instructions
    assert "maximum row limit is 50" in prompt.instructions


def test_pruning_keeps_join_partner_named_only_indirectly() -> None:
    # "customer segment" never names `accounts`, but invoices references it; dozens of
    # unrelated tables that merely share a `segment` column must not crowd it out.
    accounts = TableSchema(
        name="accounts",
        columns=[
            ColumnSchema(name="account_id", type="INTEGER", primary_key=True),
            ColumnSchema(name="segment", type="TEXT"),
        ],
    )
    invoices = TableSchema(
        name="invoices",
        columns=[
            ColumnSchema(name="invoice_id", type="INTEGER", primary_key=True),
            ColumnSchema(name="account_id", type="INTEGER"),
            ColumnSchema(name="amount_cents", type="INTEGER"),
        ],
        foreign_keys=[
            ForeignKey(columns=["account_id"], referred_table="accounts", referred_columns=["account_id"])
        ],
    )
    distractors = [
        TableSchema(
            name=f"marketing_records_{index}",
            columns=[ColumnSchema(name="segment", type="TEXT"), ColumnSchema(name="amount_cents", type="INTEGER")],
        )
        for index in range(60)
    ]
    schema = DatabaseSchema(dialect="sqlite", tables=[*distractors, invoices, accounts])

    pruned = prune_schema(schema, "Total invoiced amount per customer segment", token_budget=300)

    kept = {table.name for table in pruned.tables}
    assert {"invoices", "accounts"} <= kept
    assert len(kept) < len(schema.tables)
