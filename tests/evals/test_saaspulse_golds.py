from __future__ import annotations

from pathlib import Path

import pytest

from querypilot import QueryPilot
from querypilot.evals.loader import load_suite_dir
from querypilot.generation.sql_generator import DemoSQLGenerator

SUITE_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "saaspulse"

_SUITE = load_suite_dir(SUITE_DIR)
_CORRECTNESS = [case for case in _SUITE.cases if case.should_pass]


@pytest.fixture(scope="module")
def qp() -> QueryPilot:
    return QueryPilot.connect(
        database_url=_SUITE.resolved_fixture_db(_CORRECTNESS[0]),
        dialect="sqlite",
        generator=DemoSQLGenerator(),
    )


@pytest.mark.parametrize("case", _CORRECTNESS, ids=lambda case: case.id)
def test_gold_sql_passes_validator(qp: QueryPilot, case) -> None:
    # A gold the validator rejects can never be matched, so the case is unwinnable.
    result = qp.validate_sql(case.gold_sql)

    assert result.valid, result.blocked_reason or result.errors
