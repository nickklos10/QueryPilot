from __future__ import annotations

from pathlib import Path
from typing import Iterable

from querypilot.audit.sinks import AuditSink
from querypilot.audit.types import QueryAuditRecord
from querypilot.evals.suite import BenchmarkCase, BenchmarkSuite, ComparisonConfig, SuiteThresholds


_DEFAULT_TAGS = ("replay", "regression")
_DEFAULT_REPLAY_OPERATION = "ask"
_FETCH_OVERHEAD_FACTOR = 10  # eligible records may be a fraction of total; over-fetch to compensate

_URL_DIALECT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("sqlite:", "sqlite"),
    ("postgresql+", "postgres"),
    ("postgresql:", "postgres"),
    ("postgres:", "postgres"),
    ("mysql+", "mysql"),
    ("mysql:", "mysql"),
    ("snowflake:", "snowflake"),
    ("bigquery:", "bigquery"),
    ("redshift+", "redshift"),
    ("redshift:", "redshift"),
)


def dialect_from_url(database_url: str) -> str:
    lowered = database_url.lower()
    for prefix, dialect in _URL_DIALECT_PREFIXES:
        if lowered.startswith(prefix):
            return dialect
    return "sqlite"


def replay_from_sink(
    sink: AuditSink,
    *,
    fixture_db: str,
    fixture_dialect: str | None = None,
    suite_name: str = "audit_replay",
    only_successful: bool = True,
    skip_masked: bool = True,
    skip_empty_results: bool = True,
    min_row_count: int = 1,
    limit: int = 1000,
    extra_tags: Iterable[str] = (),
    thresholds: SuiteThresholds | None = None,
    comparison: ComparisonConfig | None = None,
) -> BenchmarkSuite:
    fetch_size = max(limit * _FETCH_OVERHEAD_FACTOR, limit)
    records = list(reversed(sink.recent(limit=fetch_size)))
    return _build_suite(
        records=records,
        fixture_db=fixture_db,
        fixture_dialect=fixture_dialect or dialect_from_url(fixture_db),
        suite_name=suite_name,
        only_successful=only_successful,
        skip_masked=skip_masked,
        skip_empty_results=skip_empty_results,
        min_row_count=min_row_count,
        limit=limit,
        extra_tags=extra_tags,
        thresholds=thresholds,
        comparison=comparison,
    )


def replay_from_jsonl(
    path: str | Path,
    *,
    fixture_db: str,
    fixture_dialect: str | None = None,
    suite_name: str = "audit_replay",
    only_successful: bool = True,
    skip_masked: bool = True,
    skip_empty_results: bool = True,
    min_row_count: int = 1,
    limit: int = 1000,
    extra_tags: Iterable[str] = (),
    thresholds: SuiteThresholds | None = None,
    comparison: ComparisonConfig | None = None,
) -> BenchmarkSuite:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Audit JSONL file not found: {target}")
    records = _read_jsonl(target)
    return _build_suite(
        records=records,
        fixture_db=fixture_db,
        fixture_dialect=fixture_dialect or dialect_from_url(fixture_db),
        suite_name=suite_name,
        only_successful=only_successful,
        skip_masked=skip_masked,
        skip_empty_results=skip_empty_results,
        min_row_count=min_row_count,
        limit=limit,
        extra_tags=extra_tags,
        thresholds=thresholds,
        comparison=comparison,
    )


def _read_jsonl(path: Path) -> list[QueryAuditRecord]:
    out: list[QueryAuditRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            out.append(QueryAuditRecord.model_validate_json(line))
    return out


def _build_suite(
    *,
    records: list[QueryAuditRecord],
    fixture_db: str,
    fixture_dialect: str,
    suite_name: str,
    only_successful: bool,
    skip_masked: bool,
    skip_empty_results: bool,
    min_row_count: int,
    limit: int,
    extra_tags: Iterable[str],
    thresholds: SuiteThresholds | None,
    comparison: ComparisonConfig | None,
) -> BenchmarkSuite:
    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    for record in records:
        if not _eligible(
            record,
            only_successful=only_successful,
            skip_masked=skip_masked,
            skip_empty_results=skip_empty_results,
            min_row_count=min_row_count,
        ):
            continue
        case = _record_to_case(record, fixture_db=fixture_db, extra_tags=extra_tags)
        if case.id in seen_ids:
            continue
        seen_ids.add(case.id)
        cases.append(case)
        if len(cases) >= limit:
            break

    return BenchmarkSuite(
        name=suite_name,
        fixture_db=fixture_db,
        fixture_dialect=fixture_dialect,
        thresholds=thresholds or SuiteThresholds(),
        comparison=comparison or ComparisonConfig(),
        cases=cases,
    )


def _has_active_policy(policy: dict | None) -> bool:
    if not policy:
        return False
    for value in policy.values():
        if value:
            return True
    return False


def _eligible(
    record: QueryAuditRecord,
    *,
    only_successful: bool,
    skip_masked: bool,
    skip_empty_results: bool,
    min_row_count: int,
) -> bool:
    if record.operation != _DEFAULT_REPLAY_OPERATION:
        return False
    if record.question is None or not record.question.strip():
        return False
    if record.rewritten_sql is None or not record.rewritten_sql.strip():
        return False
    if only_successful:
        if not record.executed:
            return False
        if record.valid is False:
            return False
        if record.error:
            return False
    if skip_masked and _has_active_policy(record.access_policy):
        return False
    if skip_empty_results:
        if record.row_count is None or record.row_count < min_row_count:
            return False
    return True


def _record_to_case(
    record: QueryAuditRecord,
    *,
    fixture_db: str,
    extra_tags: Iterable[str],
) -> BenchmarkCase:
    base_tags = list(_DEFAULT_TAGS) + list(extra_tags)
    if record.app_name:
        base_tags.append(f"app:{record.app_name}")
    return BenchmarkCase(
        id=record.audit_id,
        question=record.question,
        gold_sql=record.rewritten_sql,
        fixture_db=fixture_db,
        tags=base_tags,
        source="audit_replay",
        audit_id=record.audit_id,
        original_timestamp=record.timestamp.isoformat() if record.timestamp else None,
        original_question=record.question,
    )
