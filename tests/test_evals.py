from __future__ import annotations

from querypilot import QueryPilot
from querypilot.evals.cases import EvalCase
from querypilot.evals.runner import run_eval_cases


def test_eval_runner_scores_validation_and_safety(demo_db_url: str) -> None:
    qp = QueryPilot.connect(demo_db_url, dialect="sqlite")
    cases = [
        EvalCase(
            name="safe revenue query",
            question="Show customer revenue",
            sql="SELECT customer_name, revenue FROM customers",
            expected_tables=["customers"],
            expected_sql_contains=["LIMIT 100"],
            must_not_contain=["DROP", "UPDATE"],
            should_pass=True,
        ),
        EvalCase(
            name="drop table blocked",
            sql="DROP TABLE customers",
            should_pass=False,
        ),
    ]

    report = run_eval_cases(qp, cases)

    assert report.total == 2
    assert report.passed == 2
    assert report.failed == 0
    assert report.results[0].passed is True
    assert report.results[0].validation is not None
    assert report.results[1].passed is True
    assert report.results[1].validation is not None
