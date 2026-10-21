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
    assert "Acme Corp" in seen[0].get_table("customers").get_column("customer_name").sample_values
    # The cached schema itself is not mutated with samples.
    assert qp.get_schema().get_table("customers").get_column("customer_name").sample_values == []
