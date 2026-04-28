from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from querypilot import QueryPilot
from querypilot.audit import (
    AuditMetadata,
    InMemoryAuditSink,
    JSONLAuditSink,
    QueryAuditRecord,
)
from querypilot.evals import (
    BenchmarkSuite,
    replay_from_jsonl,
    replay_from_sink,
    run_suite,
)
from querypilot.evals.factory import build_qp_factory
from querypilot.evals.cost import NullCostTracker
from querypilot.generation.sql_generator import DemoSQLGenerator


FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "demo.db"


@pytest.fixture(scope="module")
def fixture_db_path() -> Path:
    if not FIXTURE_DB.exists():
        from tests.fixtures.seed_demo import seed

        seed(FIXTURE_DB)
    return FIXTURE_DB


@pytest.fixture()
def fixture_db_url(fixture_db_path: Path) -> str:
    return f"sqlite:///{fixture_db_path}"


def _record(
    *,
    audit_id: str = "rec-001",
    operation: str = "ask",
    question: str | None = "Top customers by revenue",
    sql: str | None = "SELECT customer_name, revenue FROM customers ORDER BY revenue DESC LIMIT 100",
    rewritten_sql: str | None = "SELECT customer_name, revenue FROM customers ORDER BY revenue DESC LIMIT 100",
    valid: bool | None = True,
    executed: bool = True,
    row_count: int | None = 5,
    error: str | None = None,
    access_policy: dict | None = None,
    app_name: str | None = None,
    timestamp: datetime | None = None,
) -> QueryAuditRecord:
    return QueryAuditRecord(
        audit_id=audit_id,
        timestamp=timestamp or datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        operation=operation,
        question=question,
        sql=sql,
        rewritten_sql=rewritten_sql,
        valid=valid,
        executed=executed,
        row_count=row_count,
        error=error,
        access_policy=access_policy or {},
        app_name=app_name,
    )


def test_replay_from_sink_returns_benchmark_suite(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="rec-1"))
    sink.write(_record(audit_id="rec-2"))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert isinstance(suite, BenchmarkSuite)
    assert {c.id for c in suite.cases} == {"rec-1", "rec-2"}
    assert suite.fixture_db == fixture_db_url


def test_replay_preserves_audit_metadata(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    ts = datetime(2026, 4, 26, 18, 32, 11, tzinfo=UTC)
    sink.write(_record(audit_id="rec-x", question="Top customers", timestamp=ts, app_name="billing-bot"))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)
    case = suite.cases[0]

    assert case.source == "audit_replay"
    assert case.audit_id == "rec-x"
    assert case.original_question == "Top customers"
    assert case.original_timestamp == ts.isoformat()
    assert "replay" in case.tags
    assert "regression" in case.tags
    assert "app:billing-bot" in case.tags


def test_replay_uses_rewritten_sql_as_gold(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(
        _record(
            audit_id="rec-1",
            sql="SELECT * FROM customers",
            rewritten_sql="SELECT * FROM customers LIMIT 100",
        )
    )

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)
    case = suite.cases[0]

    assert case.gold_sql == "SELECT * FROM customers LIMIT 100"


def test_replay_skips_non_ask_operations_by_default(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="ask-1", operation="ask"))
    sink.write(_record(audit_id="exec-1", operation="execute_sql"))
    sink.write(_record(audit_id="val-1", operation="validate_sql"))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert {c.id for c in suite.cases} == {"ask-1"}


def test_replay_skips_failed_executions_by_default(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="ok", executed=True, valid=True))
    sink.write(_record(audit_id="invalid", valid=False))
    sink.write(_record(audit_id="error", error="db connection refused"))
    sink.write(_record(audit_id="not-executed", executed=False))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert {c.id for c in suite.cases} == {"ok"}


def test_replay_include_failures_keeps_invalid_records(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="ok"))
    sink.write(_record(audit_id="invalid", valid=False))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url, only_successful=False)

    assert {c.id for c in suite.cases} == {"ok", "invalid"}


def test_replay_skips_masked_by_default(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="clean", access_policy={}))
    sink.write(
        _record(
            audit_id="masked",
            access_policy={"customers": {"email": "redact"}},
        )
    )

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert {c.id for c in suite.cases} == {"clean"}


def test_replay_include_masked_keeps_redacted_records(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(
        _record(audit_id="masked", access_policy={"customers": {"email": "redact"}})
    )

    suite = replay_from_sink(sink, fixture_db=fixture_db_url, skip_masked=False)

    assert {c.id for c in suite.cases} == {"masked"}


def test_replay_skips_empty_results_by_default(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="rows", row_count=5))
    sink.write(_record(audit_id="empty", row_count=0))
    sink.write(_record(audit_id="null-rows", row_count=None))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert {c.id for c in suite.cases} == {"rows"}


def test_replay_include_empty_keeps_zero_row_records(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="rows", row_count=5))
    sink.write(_record(audit_id="empty", row_count=0))

    suite = replay_from_sink(
        sink, fixture_db=fixture_db_url, skip_empty_results=False
    )

    assert {c.id for c in suite.cases} == {"rows", "empty"}


def test_replay_min_row_count_threshold(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="big", row_count=10))
    sink.write(_record(audit_id="small", row_count=2))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url, min_row_count=5)

    assert {c.id for c in suite.cases} == {"big"}


def test_replay_limit_caps_case_count(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    for i in range(10):
        sink.write(_record(audit_id=f"rec-{i}"))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url, limit=3)

    assert len(suite.cases) == 3


def test_replay_deduplicates_by_audit_id(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="dupe"))
    sink.write(_record(audit_id="dupe"))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert len(suite.cases) == 1


def test_replay_extra_tags_appended(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="rec-1"))

    suite = replay_from_sink(
        sink, fixture_db=fixture_db_url, extra_tags=["weekly-batch"]
    )

    assert "weekly-batch" in suite.cases[0].tags


def test_replay_drops_records_with_blank_question(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="ok"))
    sink.write(_record(audit_id="empty-q", question=""))
    sink.write(_record(audit_id="none-q", question=None))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert {c.id for c in suite.cases} == {"ok"}


def test_replay_drops_records_with_blank_rewritten_sql(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="ok"))
    sink.write(_record(audit_id="empty-sql", rewritten_sql=""))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert {c.id for c in suite.cases} == {"ok"}


def test_replay_from_jsonl_reads_file(tmp_path: Path, fixture_db_url: str) -> None:
    audit_path = tmp_path / "audit.jsonl"
    sink = JSONLAuditSink(audit_path)
    sink.write(_record(audit_id="rec-1"))
    sink.write(_record(audit_id="rec-2"))

    suite = replay_from_jsonl(audit_path, fixture_db=fixture_db_url)

    assert {c.id for c in suite.cases} == {"rec-1", "rec-2"}


def test_replay_from_jsonl_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        replay_from_jsonl(tmp_path / "nope.jsonl", fixture_db="sqlite:///x.db")


def test_replay_from_jsonl_skips_blank_lines(tmp_path: Path, fixture_db_url: str) -> None:
    audit_path = tmp_path / "audit.jsonl"
    sink = JSONLAuditSink(audit_path)
    sink.write(_record(audit_id="rec-1"))
    audit_path.write_text(audit_path.read_text() + "\n\n", encoding="utf-8")

    suite = replay_from_jsonl(audit_path, fixture_db=fixture_db_url)

    assert {c.id for c in suite.cases} == {"rec-1"}


def test_replay_round_trip_against_real_querypilot(
    tmp_path: Path, fixture_db_path: Path, fixture_db_url: str
) -> None:
    # 1. Run several real ask() calls; capture audit records
    audit_path = tmp_path / "audit.jsonl"
    sink = JSONLAuditSink(audit_path)
    qp = QueryPilot.connect(
        database_url=fixture_db_url,
        dialect="sqlite",
        audit_sink=sink,
        audit_metadata=AuditMetadata(app_name="test-suite"),
    )

    questions = [
        "Top customers by revenue",
        "Count of customers",
        "Show invoices",
    ]
    for q in questions:
        qp.ask(q)

    # 2. Replay the audit log into a regression suite
    suite = replay_from_jsonl(audit_path, fixture_db=fixture_db_url)

    assert len(suite.cases) == len(questions)
    assert all(c.source == "audit_replay" for c in suite.cases)
    assert all("app:test-suite" in c.tags for c in suite.cases)

    # 3. Run the replayed suite through run_suite. Every case should pass
    # because gold_sql == the SQL that previously executed against the same DB.
    qp_factory = build_qp_factory(
        database_url=fixture_db_url,
        generator=DemoSQLGenerator(),
    )
    report = run_suite(
        suite,
        qp_factory=qp_factory,
        cost_tracker_factory=NullCostTracker,
    )

    assert report.passed == len(questions)
    assert report.pass_rate == 1.0


def test_replay_preserves_only_first_appearance_of_duplicate_id(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="dupe", question="first"))
    sink.write(_record(audit_id="dupe", question="second"))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert len(suite.cases) == 1
    assert suite.cases[0].original_question == "first"


def test_replay_infers_sqlite_dialect_from_url(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record())

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert suite.fixture_dialect == "sqlite"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("sqlite:///x.db", "sqlite"),
        ("postgresql://u:p@host/db", "postgres"),
        ("postgres://u:p@host/db", "postgres"),
        ("postgresql+psycopg://u:p@host/db", "postgres"),
        ("mysql://u:p@host/db", "mysql"),
        ("mysql+pymysql://u:p@host/db", "mysql"),
        ("snowflake://acct/db", "snowflake"),
        ("bigquery://project/dataset", "bigquery"),
        ("redshift+psycopg2://u:p@host/db", "redshift"),
        ("unknownscheme://x", "sqlite"),  # falls back to sqlite default
    ],
)
def test_dialect_from_url(url: str, expected: str) -> None:
    from querypilot.evals.replay import dialect_from_url

    assert dialect_from_url(url) == expected


def test_replay_explicit_fixture_dialect_overrides_url(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record())

    suite = replay_from_sink(
        sink, fixture_db=fixture_db_url, fixture_dialect="postgres"
    )

    assert suite.fixture_dialect == "postgres"


def test_replay_postgres_url_yields_postgres_dialect() -> None:
    sink = InMemoryAuditSink()
    sink.write(_record())

    suite = replay_from_sink(
        sink, fixture_db="postgresql://user:pw@host/db"
    )

    assert suite.fixture_dialect == "postgres"


def test_replay_from_sink_fetches_more_than_default_when_limit_high(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    # Generate 50 eligible records then write them oldest-first to the sink.
    for i in range(50):
        sink.write(_record(audit_id=f"rec-{i:03d}"))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url, limit=50)

    assert len(suite.cases) == 50


def test_replay_does_not_set_source_authored(fixture_db_url: str) -> None:
    sink = InMemoryAuditSink()
    sink.write(_record(audit_id="rec-1"))

    suite = replay_from_sink(sink, fixture_db=fixture_db_url)

    assert suite.cases[0].source == "audit_replay"
