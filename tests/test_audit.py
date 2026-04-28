from __future__ import annotations

import json
from pathlib import Path

from querypilot import QueryPilot
from querypilot.audit import AuditMetadata, JSONLAuditSink


def test_querypilot_records_validate_and_execute_audit_events(demo_db_url: str) -> None:
    qp = QueryPilot.connect(
        demo_db_url,
        dialect="sqlite",
        audit_metadata=AuditMetadata(actor="analyst", session_id="s1", app_name="tests"),
    )

    validation = qp.validate_sql("SELECT customer_name FROM customers")
    result = qp.execute_sql("SELECT customer_name FROM customers")
    records = qp.get_audit_records()

    assert validation.audit_id is not None
    assert result.audit_id is not None
    assert [record.operation for record in records] == ["execute_sql", "validate_sql"]
    assert records[0].actor == "analyst"
    assert records[0].session_id == "s1"
    assert records[0].app_name == "tests"
    assert records[0].executed is True
    assert records[0].row_count == 3
    assert records[1].sql == "SELECT customer_name FROM customers"
    assert records[1].valid is True


def test_ask_returns_audit_id_and_records_full_flow(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2)

    answer = qp.ask("Which customers generated the most revenue?")
    records = qp.get_audit_records()

    assert answer.audit_id is not None
    assert records[0].audit_id == answer.audit_id
    assert records[0].operation == "ask"
    assert records[0].question == "Which customers generated the most revenue?"
    assert records[0].executed is True
    assert records[0].row_count == 2
    assert records[0].validation is not None


def test_jsonl_audit_sink_persists_records(demo_db_url: str, tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    qp = QueryPilot.connect(
        demo_db_url,
        dialect="sqlite",
        audit_sink=JSONLAuditSink(audit_path),
    )

    result = qp.execute_sql("SELECT customer_name FROM customers LIMIT 1")

    lines = audit_path.read_text().splitlines()
    payload = json.loads(lines[0])
    assert payload["audit_id"] == result.audit_id
    assert payload["operation"] == "execute_sql"
    assert payload["executed"] is True
