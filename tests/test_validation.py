from __future__ import annotations

import pytest

from querypilot import QueryPilot
from querypilot.core.config import QueryPilotConfig, SafetyPolicy
from querypilot.core.types import ColumnSchema, DatabaseSchema, TableSchema
from querypilot.validation.validator import SQLValidator


def test_select_is_rewritten_with_limit(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2)

    result = qp.validate_sql("SELECT * FROM customers")

    assert result.valid is True
    assert result.readonly is True
    assert result.limit_applied is True
    assert result.rewritten_sql == "SELECT * FROM customers LIMIT 2"
    assert result.tables == ["customers"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "UPDATE customers SET arr = 0",
        "INSERT INTO customers (customer_name, arr, revenue) VALUES ('x', 1, 1)",
        "DELETE FROM customers",
        "CREATE TABLE unsafe (id INTEGER)",
        "ALTER TABLE customers ADD COLUMN unsafe TEXT",
        "TRUNCATE TABLE customers",
        "GRANT SELECT ON customers TO analyst",
        "REVOKE SELECT ON customers FROM analyst",
        "COPY customers TO STDOUT",
    ],
)
def test_dangerous_sql_is_rejected(demo_db_url: str, sql: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql(sql)

    assert result.valid is False
    assert result.readonly is False
    assert result.errors


def test_unknown_table_is_rejected(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql("SELECT * FROM payments")

    assert result.valid is False
    assert "Unknown table: payments" in result.errors


def test_blocked_table_is_rejected(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", blocked_tables=["invoices"])

    result = qp.validate_sql("SELECT * FROM invoices")

    assert result.valid is False
    assert "Blocked table referenced: invoices" in result.errors


def test_allowed_table_policy_is_enforced(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", allowed_tables=["customers"])

    result = qp.validate_sql("SELECT * FROM invoices")

    assert result.valid is False
    assert "Table is not in allowed_tables: invoices" in result.errors


def test_existing_limit_is_preserved_or_capped(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=10)

    small = qp.validate_sql("SELECT * FROM customers LIMIT 3")
    large = qp.validate_sql("SELECT * FROM customers LIMIT 500")

    assert small.rewritten_sql == "SELECT * FROM customers LIMIT 3"
    assert small.limit_applied is False
    assert large.rewritten_sql == "SELECT * FROM customers LIMIT 10"
    assert large.limit_applied is True


def test_validation_includes_policy_checks_risk_and_fingerprint(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=10)

    result = qp.validate_sql("SELECT customer_name FROM customers")

    assert result.risk_level == "low"
    assert result.query_fingerprint
    assert {check.name for check in result.policy_checks} >= {
        "parseable",
        "single_statement",
        "readonly_select",
        "known_tables",
        "row_limit",
    }
    assert all(check.passed for check in result.policy_checks)


def test_multi_statement_sql_is_rejected(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql("SELECT * FROM customers; SELECT * FROM invoices")

    assert result.valid is False
    assert result.risk_level == "critical"
    assert result.blocked_reason == "Multiple SQL statements are not allowed."
    assert any(check.name == "single_statement" and not check.passed for check in result.policy_checks)


def test_malformed_sql_returns_structured_validation_failure(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql("SELECT FROM")

    assert result.valid is False
    assert result.risk_level == "critical"
    assert result.blocked_reason is not None
    assert result.blocked_reason.startswith("SQL parse error:")
    assert any(check.name == "parseable" and not check.passed for check in result.policy_checks)


def test_select_star_warns_by_default_and_can_be_rejected(demo_db_url: str) -> None:
    permissive = QueryPilot.connect(demo_db_url, dialect="sqlite")
    strict = QueryPilot.connect(
        demo_db_url,
        dialect="sqlite",
        safety_policy=SafetyPolicy(allow_select_star=False),
    )

    permissive_result = permissive.validate_sql("SELECT * FROM customers")
    strict_result = strict.validate_sql("SELECT * FROM customers")

    assert permissive_result.valid is True
    assert "SELECT * may expose more data than intended." in permissive_result.warnings
    assert permissive_result.risk_level == "medium"
    assert strict_result.valid is False
    assert strict_result.blocked_reason == "SELECT * is disabled by policy."


def test_cartesian_join_is_rejected(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql("SELECT customers.id, invoices.id FROM customers, invoices")

    assert result.valid is False
    assert result.risk_level == "high"
    assert result.blocked_reason == "Potential Cartesian join detected."
    assert any(check.name == "join_safety" and not check.passed for check in result.policy_checks)


def test_dangerous_word_inside_literal_or_comment_is_allowed(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    literal = qp.validate_sql("SELECT 'drop' AS harmless FROM customers")
    comment = qp.validate_sql("SELECT customer_name FROM customers -- drop is documentation")

    assert literal.valid is True
    assert comment.valid is True


def test_nested_write_expression_is_rejected(demo_db_url: str) -> None:
    schema = QueryPilot.connect(demo_db_url, dialect="sqlite").get_schema()
    validator = SQLValidator(QueryPilotConfig(dialect="postgres"))

    result = validator.validate(
        "WITH changed AS (DELETE FROM customers RETURNING id) SELECT * FROM changed",
        schema,
    )

    assert result.valid is False
    assert result.blocked_reason == "SQL contains a non-read-only operation: DELETE"


@pytest.mark.parametrize(
    "function_name",
    [
        "nextval",
        "setval",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_sleep",
    ],
)
def test_dangerous_postgres_functions_are_rejected(function_name: str) -> None:
    validator = SQLValidator(QueryPilotConfig(dialect="postgres"))

    result = validator.validate(
        f"SELECT {function_name}('x')",
        DatabaseSchema(dialect="postgres"),
    )

    assert result.valid is False
    assert result.blocked_reason == f"SQL function is blocked by policy: {function_name}"


def _schema_with_tables(dialect: str, *names: str) -> DatabaseSchema:
    return DatabaseSchema(
        dialect=dialect,
        tables=[
            TableSchema(name=name, columns=[ColumnSchema(name="id", type="INTEGER")])
            for name in names
        ],
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM 'data.csv'",
        "SELECT * FROM \"data.csv\"",
        "SELECT p.id FROM 's3://bucket/x.parquet' AS p",
        "SELECT * FROM 'https://example.com/x.json'",
        "SELECT * FROM customers JOIN '/etc/passwd.csv' AS f ON f.id = customers.id",
    ],
)
def test_file_and_url_table_references_are_rejected(sql: str) -> None:
    # A schema table named like the file's extension must not let the read through.
    validator = SQLValidator(QueryPilotConfig(dialect="duckdb"))
    schema = _schema_with_tables("duckdb", "csv", "parquet", "json", "customers")

    result = validator.validate(sql, schema)

    assert result.valid is False
    assert result.readonly is False
    assert result.blocked_reason.startswith("Table reference looks like a file path or URL:")


def test_schema_qualified_table_is_not_mistaken_for_a_path() -> None:
    validator = SQLValidator(QueryPilotConfig(dialect="duckdb"))

    result = validator.validate(
        "SELECT id FROM main.customers", _schema_with_tables("duckdb", "customers")
    )

    assert result.valid is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id /*!50000 , SLEEP(5) */ FROM t",
        "SELECT id FROM t /*!UNION SELECT password FROM users */",
        "SELECT id /*M!100000 , SLEEP(5) */ FROM t",
    ],
)
def test_mysql_executable_comments_are_rejected(sql: str) -> None:
    validator = SQLValidator(QueryPilotConfig(dialect="mysql"))

    result = validator.validate(sql, _schema_with_tables("mysql", "t"))

    assert result.valid is False
    assert result.blocked_reason == "Executable comments (/*! ... */) are not allowed."


def test_ordinary_and_hint_comments_are_allowed() -> None:
    validator = SQLValidator(QueryPilotConfig(dialect="mysql"))
    schema = _schema_with_tables("mysql", "t")

    assert validator.validate("SELECT id /* plain note */ FROM t", schema).valid is True
    assert validator.validate("SELECT /*+ MAX_EXECUTION_TIME(1000) */ id FROM t", schema).valid


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM OPENROWSET(BULK 'C:\\secrets.txt', SINGLE_CLOB) AS x",
        "SELECT * FROM OPENROWSET('SQLNCLI', 'Server=evil;', 'SELECT 1') AS x",
        "SELECT * FROM OPENQUERY(linked, 'SELECT 1')",
        "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Data Source=evil').db.dbo.t",
    ],
)
def test_tsql_external_rowsets_are_rejected(sql: str) -> None:
    validator = SQLValidator(QueryPilotConfig(dialect="tsql"))

    result = validator.validate(sql, _schema_with_tables("tsql", "t", "x"))

    assert result.valid is False


def test_function_allowlist_fails_closed() -> None:
    validator = SQLValidator(
        QueryPilotConfig(
            dialect="postgres",
            safety_policy=SafetyPolicy(allowed_functions=["count"]),
        )
    )
    schema = DatabaseSchema(dialect="postgres")

    assert validator.validate("SELECT COUNT(*)", schema).valid is True
    result = validator.validate("SELECT LOWER('A')", schema)

    assert result.valid is False
    assert result.blocked_reason == "SQL function is not allowed by policy: lower"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_name, COUNT(*) AS n FROM customers GROUP BY customer_name ORDER BY n DESC",
        "SELECT customer_name, SUM(revenue) AS rev FROM customers GROUP BY customer_name HAVING rev > 0",
        "SELECT UPPER(customer_name) AS seg, COUNT(*) AS n FROM customers GROUP BY seg ORDER BY seg",
        "WITH big AS (SELECT customer_name, revenue FROM customers) SELECT customer_name FROM big",
        "WITH big AS (SELECT * FROM customers) SELECT b.arr FROM big AS b",
        "SELECT customer_name FROM customers UNION SELECT month FROM invoices",
        "SELECT customer_name FROM customers UNION ALL SELECT month FROM invoices ORDER BY 1",
        "SELECT t.seg FROM (SELECT customer_name AS seg FROM customers) AS t",
        "SELECT revenue FROM invoices WHERE customer_id IN (SELECT id FROM customers)",
        (
            "SELECT c.customer_name FROM customers AS c WHERE EXISTS "
            "(SELECT 1 FROM invoices AS i WHERE i.customer_id = c.id)"
        ),
        "SELECT customer_name, RANK() OVER (ORDER BY revenue DESC) AS r FROM customers",
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT n FROM r",
        (
            "SELECT t.customer_name FROM (SELECT c.customer_name, i.revenue FROM customers AS c "
            "JOIN invoices AS i ON i.customer_id = c.id) AS t"
        ),
    ],
)
def test_common_read_only_query_shapes_are_accepted(demo_db_url: str, sql: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql(sql)

    assert result.errors == []
    assert result.valid is True
    qp.execute_sql(sql)


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("WITH big AS (SELECT customer_name FROM customers) SELECT revenue FROM big", "Unknown column: revenue"),
        ("WITH big AS (SELECT missing FROM customers) SELECT * FROM big", "Unknown column: missing"),
        ("SELECT t.nope FROM (SELECT customer_name FROM customers) AS t", "Unknown column: nope"),
        ("SELECT z.customer_name FROM customers AS c", "Unknown table qualifier: z"),
        ("SELECT customer_name FROM customers UNION SELECT missing FROM invoices", "Unknown column: missing"),
        ("WITH big AS (SELECT * FROM payments) SELECT * FROM big", "Unknown table: payments"),
    ],
)
def test_scope_aware_resolution_still_fails_closed(demo_db_url: str, sql: str, error: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql(sql)

    assert result.valid is False
    assert error in result.errors


def test_cte_column_list_names_recursive_cte_outputs(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql(
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT n FROM r"
    )

    assert result.errors == []
    assert result.tables == []


def test_cte_names_are_not_reported_as_tables(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    result = qp.validate_sql(
        "WITH big AS (SELECT customer_name FROM customers) SELECT customer_name FROM big"
    )

    assert result.tables == ["customers"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM invoices LIMIT -1",
        "SELECT id FROM invoices LIMIT 1000 + 1000",
        "SELECT id FROM invoices LIMIT (SELECT 5000)",
        "SELECT id FROM invoices UNION ALL SELECT id FROM customers",
        "SELECT id FROM invoices UNION ALL SELECT id FROM customers LIMIT 500",
    ],
)
def test_row_limit_cannot_be_bypassed(demo_db_url: str, sql: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2)

    result = qp.execute_sql(sql)

    assert result.row_count <= 2
    assert result.sql.endswith("LIMIT 2")


def test_select_into_is_rejected() -> None:
    validator = SQLValidator(QueryPilotConfig(dialect="postgres"))

    result = validator.validate(
        "SELECT * INTO stolen FROM customers",
        DatabaseSchema(dialect="postgres"),
    )

    assert result.valid is False
    assert result.blocked_reason == "SQL contains a non-read-only operation: INTO"
