from __future__ import annotations

import pytest

from querypilot import QueryPilot


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
