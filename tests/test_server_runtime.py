from __future__ import annotations

from fastapi.testclient import TestClient

from querypilot.access import AccessPolicy
from querypilot.server.app import create_app


def test_server_schema_validate_execute_and_ask_endpoints(demo_db_url: str) -> None:
    app = create_app(database_url=demo_db_url, dialect="sqlite", max_rows=2)
    client = TestClient(app)

    schema_response = client.get("/schema")
    validate_response = client.post("/validate-sql", json={"sql": "SELECT * FROM customers"})
    execute_response = client.post(
        "/execute-sql",
        json={"sql": "SELECT customer_name, revenue FROM customers ORDER BY revenue DESC"},
    )
    ask_response = client.post(
        "/ask",
        json={"question": "Which customers generated the most revenue?"},
    )

    assert schema_response.status_code == 200
    assert schema_response.json()["tables"][0]["name"] == "customers"
    assert validate_response.status_code == 200
    assert validate_response.json()["rewritten_sql"] == "SELECT * FROM customers LIMIT 2"
    assert execute_response.status_code == 200
    assert execute_response.json()["row_count"] == 2
    assert ask_response.status_code == 200
    assert ask_response.json()["validation"]["valid"] is True


def test_server_returns_400_for_unsafe_execution(demo_db_url: str) -> None:
    app = create_app(database_url=demo_db_url, dialect="sqlite")
    client = TestClient(app)

    response = client.post("/execute-sql", json={"sql": "DROP TABLE customers"})

    assert response.status_code == 400
    assert "SQL validation failed" in response.json()["detail"]


def test_server_eval_endpoint(demo_db_url: str) -> None:
    app = create_app(database_url=demo_db_url, dialect="sqlite")
    client = TestClient(app)

    response = client.post(
        "/evals/run",
        json={
            "cases": [
                {
                    "name": "safe query",
                    "sql": "SELECT customer_name FROM customers",
                    "expected_tables": ["customers"],
                    "should_pass": True,
                },
                {
                    "name": "unsafe query",
                    "sql": "DROP TABLE customers",
                    "should_pass": False,
                },
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["passed"] == 2


def test_create_app_accepts_existing_querypilot(demo_db_url: str) -> None:
    from querypilot import QueryPilot

    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")
    app = create_app(querypilot=qp)
    client = TestClient(app)

    response = client.post("/search-schema", json={"query": "customers"})

    assert response.status_code == 200
    assert response.json()[0]["table"] == "customers"


def test_server_propagates_audit_metadata_and_lists_recent_records(demo_db_url: str) -> None:
    app = create_app(database_url=demo_db_url, dialect="sqlite")
    client = TestClient(app)

    response = client.post(
        "/execute-sql",
        json={
            "sql": "SELECT customer_name FROM customers LIMIT 1",
            "metadata": {
                "actor": "agent-1",
                "session_id": "session-1",
                "app_name": "dashboard",
                "trace_id": "trace-1",
            },
        },
    )
    recent = client.get("/audit/recent")

    assert response.status_code == 200
    assert response.json()["audit_id"] is not None
    assert recent.status_code == 200
    assert recent.json()[0]["audit_id"] == response.json()["audit_id"]
    assert recent.json()[0]["actor"] == "agent-1"
    assert recent.json()[0]["trace_id"] == "trace-1"


def test_server_enforces_access_policy(demo_db_url: str) -> None:
    app = create_app(
        database_url=demo_db_url,
        dialect="sqlite",
        access_policy=AccessPolicy(blocked_columns={"customers": ["revenue"]}),
    )
    client = TestClient(app)

    response = client.post("/validate-sql", json={"sql": "SELECT revenue FROM customers"})

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["blocked_reason"] == (
        "Column is blocked by access policy: customers.revenue"
    )
