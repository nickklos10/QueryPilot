from __future__ import annotations

import threading

import pytest

from querypilot import QueryPilot
from querypilot.audit import AuditMetadata


def test_search_schema_returns_relevant_matches(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    matches = qp.search_schema("customer revenue")

    assert matches
    assert matches[0].table == "customers"


def test_ask_returns_answer_for_supported_offline_question(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite", max_rows=2)

    answer = qp.ask("Which customers generated the most revenue?")

    assert answer.question == "Which customers generated the most revenue?"
    assert answer.sql == "SELECT customer_name, revenue FROM customers ORDER BY revenue DESC LIMIT 2"
    assert answer.rows[0]["customer_name"] == "Acme Corp"
    assert "revenue" in answer.explanation.lower()
    assert answer.validation.valid is True
    assert answer.execution_time_ms >= 0


def test_unsupported_offline_question_returns_structured_error(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    with pytest.raises(ValueError, match="Could not generate SQL"):
        qp.ask("What is the churn forecast after the pricing change?")

    generated = qp.generate_sql("What is the churn forecast after the pricing change?")
    assert generated.sql is None
    assert generated.errors


def test_search_schema_matches_singular_question_words(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")

    assert qp.search_schema("revenue by customer")[0].table == "customers"
    assert qp.search_schema("monthly invoice")[0].table == "invoices"


def test_ask_uses_prompt_schema_with_samples_and_hints(demo_db_url: str) -> None:
    seen = []

    class _Recorder:
        def generate(self, question, schema, max_rows):
            seen.append(schema)
            from querypilot.core.types import GeneratedSQL

            return GeneratedSQL(question=question, sql="SELECT customer_name FROM customers")

    qp = QueryPilot.connect(
        demo_db_url, generator=_Recorder(), hints=["ARR is annual recurring revenue."]
    )
    qp.ask("customer names")

    assert seen[0].hints == ["ARR is annual recurring revenue."]
    assert "2026-04" in seen[0].get_table("invoices").get_column("month").sample_values
    # Names are never sampled.
    assert seen[0].get_table("customers").get_column("customer_name").sample_values == []
    # The cached schema itself is not mutated with samples.
    assert qp.get_schema().get_table("customers").get_column("customer_name").sample_values == []


def test_schema_cache_is_shared_with_audit_scoped_copies(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url)
    calls = []
    introspect = qp.connector.get_schema

    def counting_get_schema():
        calls.append(1)
        return introspect()

    qp.connector.get_schema = counting_get_schema
    # Copies made before the first load (one per request in the server) share it.
    early = qp.with_audit_metadata(AuditMetadata(actor="a"))
    threads = [threading.Thread(target=early.get_schema) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert qp.get_schema() is early.get_schema()
    assert len(calls) == 1

    late = qp.with_audit_metadata(AuditMetadata(actor="b"))
    refreshed = late.refresh_schema()

    assert len(calls) == 2
    assert qp.get_schema() is refreshed and early.get_schema() is refreshed
