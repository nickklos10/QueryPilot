"""Convert locally-downloaded Spider 1.0 / BIRD dev sets into QueryPilot suites.

This module is *file-based only*: the user downloads and extracts the dataset
themselves (both Spider and BIRD are CC BY-SA — QueryPilot does not vendor or
fetch them), and the importer takes a filesystem path. There is no network
access here.

Multi-db output shape
---------------------
A :class:`~querypilot.evals.suite.BenchmarkSuite` binds to exactly one
``fixture_db``, and :func:`querypilot.evals.loader.load_suite_dir` enforces that
every file merged from a directory shares the *same* ``fixture_db`` /
``fixture_dialect`` / ``thresholds`` / ``comparison``. Spider and BIRD, however,
each span many ``db_id``s — one SQLite file per database. They therefore cannot
be represented as a single directory-suite.

The natural mapping, and the one this importer produces, is **one self-contained
suite YAML per ``db_id``** written into an output directory:

    <output_dir>/
        spider_dev_concert_singer.yaml   # fixture_db -> .../concert_singer.sqlite
        spider_dev_pets_1.yaml           # fixture_db -> .../pets_1.sqlite
        ...

Each file is a complete ``BenchmarkSuite`` bound to that database's SQLite file,
so every one runs standalone through ``querypilot eval run --suite <file>`` /
:func:`querypilot.evals.suite_runner.run_suite`. A whole imported dataset is a
*directory of per-db suites*; you evaluate it by iterating the files (the
benchmark run-matrix loops over them), **not** by pointing ``eval run`` at the
directory — that would deliberately trip ``load_suite_dir``'s shared-fixture_db
guard, because the databases genuinely differ.

Case ids are stable (``spider_dev_0042`` / ``bird_dev_0042``), derived from the
record's original position in ``dev.json``, so ``--limit`` / ``--db`` filtering
never renumbers a case.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from querypilot.evals.loader import write_suite
from querypilot.evals.suite import BenchmarkCase, BenchmarkSuite

logger = logging.getLogger(__name__)

DatasetFormat = Literal["spider", "bird"]

_SAFE_FILENAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class DatasetImportError(ValueError):
    """Raised when a Spider/BIRD dataset cannot be read or imported."""


@dataclass(frozen=True)
class _FormatSpec:
    """Per-format layout: where the SQLite files live and which keys hold gold."""

    name: DatasetFormat
    db_subdir: str
    gold_key: str
    id_prefix: str


_SPIDER = _FormatSpec(name="spider", db_subdir="database", gold_key="query", id_prefix="spider_dev")
_BIRD = _FormatSpec(name="bird", db_subdir="dev_databases", gold_key="SQL", id_prefix="bird_dev")
_SPEC_BY_NAME: dict[str, _FormatSpec] = {_SPIDER.name: _SPIDER, _BIRD.name: _BIRD}


@dataclass
class ImportResult:
    """Summary of an import run (internal-only, so a plain dataclass)."""

    dataset_format: DatasetFormat
    output_dir: Path
    total_records: int
    imported_cases: int
    written_suites: list[Path] = field(default_factory=list)
    skipped_missing_fixture: int = 0
    skipped_bad_gold: int = 0
    skipped_malformed: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def db_count(self) -> int:
        return len(self.written_suites)


def detect_format(dataset_dir: str | Path) -> DatasetFormat:
    """Guess the dataset format from directory layout, then from record keys."""
    root = Path(dataset_dir)
    if (root / _BIRD.db_subdir).is_dir():
        return "bird"
    if (root / _SPIDER.db_subdir).is_dir():
        return "spider"

    records = _load_dev_json(_find_dev_json(root))
    if records:
        first = records[0]
        if _BIRD.gold_key in first:
            return "bird"
        if _SPIDER.gold_key in first:
            return "spider"
    raise DatasetImportError(
        f"Could not detect dataset format in {root}. Expected a 'database/' "
        "(Spider) or 'dev_databases/' (BIRD) subdirectory, or a dev.json whose "
        "records carry a 'query' (Spider) or 'SQL' (BIRD) field. "
        "Pass an explicit format to override."
    )


def import_dataset(
    dataset_dir: str | Path,
    output_dir: str | Path,
    *,
    dataset_format: DatasetFormat | None = None,
    limit: int | None = None,
    db_ids: list[str] | None = None,
    strict: bool = False,
    name_prefix: str | None = None,
) -> ImportResult:
    """Import a Spider/BIRD dev set into one ``BenchmarkSuite`` YAML per db_id.

    Parameters
    ----------
    dataset_dir:
        Extracted dataset directory containing ``dev.json`` and the per-db
        SQLite files (``database/<db_id>/<db_id>.sqlite`` for Spider,
        ``dev_databases/<db_id>/<db_id>.sqlite`` for BIRD).
    output_dir:
        Directory to write per-db suite YAML files into (created if missing).
    dataset_format:
        ``"spider"`` or ``"bird"``. Auto-detected when ``None``.
    limit:
        Cap the number of *imported* cases (records that fail validation do not
        count against it).
    db_ids:
        If given, only import cases whose ``db_id`` is in this list.
    strict:
        Error (raise ``DatasetImportError``) instead of warn+skip when a fixture
        SQLite file is missing or a gold query fails to execute.
    name_prefix:
        Override the id/name prefix (defaults to ``spider_dev`` / ``bird_dev``).
    """
    root = Path(dataset_dir)
    if not root.is_dir():
        raise DatasetImportError(f"Dataset directory not found: {root}")

    fmt = dataset_format or detect_format(root)
    if fmt not in _SPEC_BY_NAME:
        raise DatasetImportError(f"Unknown dataset format: {fmt!r}. Use 'spider' or 'bird'.")
    spec = _SPEC_BY_NAME[fmt]
    prefix = name_prefix or spec.id_prefix
    db_filter = set(db_ids) if db_ids else None

    records = _load_dev_json(_find_dev_json(root))

    result = ImportResult(
        dataset_format=spec.name,
        output_dir=Path(output_dir),
        total_records=len(records),
        imported_cases=0,
    )

    cases_by_db: dict[str, list[BenchmarkCase]] = {}
    fixture_by_db: dict[str, str] = {}
    engine_cache: dict[str, tuple[str, Engine] | None] = {}

    try:
        for index, record in enumerate(records):
            if limit is not None and result.imported_cases >= limit:
                break
            if not isinstance(record, dict):
                _skip(result, "skipped_malformed", f"record {index}: not an object", strict)
                continue

            db_id = record.get("db_id")
            question = record.get("question")
            gold_sql = record.get(spec.gold_key)
            if not db_id or not question or not gold_sql:
                _skip(
                    result,
                    "skipped_malformed",
                    f"record {index}: missing db_id/question/{spec.gold_key}",
                    strict,
                )
                continue
            if db_filter is not None and db_id not in db_filter:
                continue

            resolved = _resolve_fixture(db_id, root, spec, engine_cache, result, strict)
            if resolved is None:
                result.skipped_missing_fixture += 1
                continue
            url, engine = resolved

            ok, error = _gold_executes(engine, gold_sql)
            if not ok:
                result.skipped_bad_gold += 1
                _skip(
                    result,
                    None,
                    f"{prefix}_{index:04d} ({db_id}): gold SQL failed to execute: {error}",
                    strict,
                )
                continue

            case = _build_case(spec, prefix, index, db_id, str(question), str(gold_sql), record)
            cases_by_db.setdefault(db_id, []).append(case)
            fixture_by_db[db_id] = url
            result.imported_cases += 1
    finally:
        for cached in engine_cache.values():
            if cached is not None:
                cached[1].dispose()

    result.written_suites = _write_suites(cases_by_db, fixture_by_db, prefix, Path(output_dir))
    return result


def _write_suites(
    cases_by_db: dict[str, list[BenchmarkCase]],
    fixture_by_db: dict[str, str],
    prefix: str,
    output_dir: Path,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for db_id, cases in cases_by_db.items():
        suite = BenchmarkSuite(
            name=f"{prefix}_{db_id}",
            fixture_db=fixture_by_db[db_id],
            fixture_dialect="sqlite",
            cases=cases,
        )
        out_path = output_dir / f"{prefix}_{_safe_filename(db_id)}.yaml"
        write_suite(suite, out_path)
        written.append(out_path)
    return written


def _resolve_fixture(
    db_id: str,
    root: Path,
    spec: _FormatSpec,
    engine_cache: dict[str, tuple[str, Engine] | None],
    result: ImportResult,
    strict: bool,
) -> tuple[str, Engine] | None:
    """Resolve (and cache) a db's SQLite fixture; None when it is missing."""
    if db_id in engine_cache:
        return engine_cache[db_id]

    db_path = root / spec.db_subdir / db_id / f"{db_id}.sqlite"
    if not db_path.is_file():
        message = f"fixture SQLite not found for db_id {db_id!r}: {db_path}"
        if strict:
            raise DatasetImportError(message)
        result.warnings.append(message)
        logger.warning(message)
        engine_cache[db_id] = None
        return None

    url = f"sqlite:///{db_path.resolve()}"
    engine = create_engine(url)
    engine_cache[db_id] = (url, engine)
    return engine_cache[db_id]


def _gold_executes(engine: Engine, sql: str) -> tuple[bool, str | None]:
    """Return whether ``sql`` runs against ``engine``.

    Mirrors ``querypilot.evals.pipeline._execute_gold`` (``text(sql)`` through the
    same connector layer) so a gold query skipped here is exactly one that would
    fail at run time. A single row is fetched to force query execution without
    materializing large result sets.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text(sql)).fetchmany(1)
        return True, None
    except Exception as exc:  # noqa: BLE001 - dirty gold SQL is expected; tolerate anything.
        return False, f"{type(exc).__name__}: {exc}"


def _build_case(
    spec: _FormatSpec,
    prefix: str,
    index: int,
    db_id: str,
    question: str,
    gold_sql: str,
    record: dict[str, Any],
) -> BenchmarkCase:
    final_question = question.strip()
    notes: str | None = None

    evidence = record.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        evidence = evidence.strip()
        # BIRD ships an "evidence" hint the model is meant to use, so fold it
        # into the question the generator sees, and keep it in notes for
        # provenance.
        final_question = f"{final_question}\n\nEvidence: {evidence}"
        notes = evidence

    return BenchmarkCase(
        id=f"{prefix}_{index:04d}",
        question=final_question,
        gold_sql=gold_sql.strip(),
        tags=[spec.name, db_id],
        notes=notes,
    )


def _skip(result: ImportResult, counter: str | None, message: str, strict: bool) -> None:
    if strict:
        raise DatasetImportError(message)
    if counter is not None:
        setattr(result, counter, getattr(result, counter) + 1)
    result.warnings.append(message)
    logger.warning(message)


def _find_dev_json(dataset_dir: Path) -> Path:
    candidate = dataset_dir / "dev.json"
    if candidate.is_file():
        return candidate
    raise DatasetImportError(
        f"No dev.json found in {dataset_dir}. Point --dataset at the extracted "
        "Spider/BIRD dev directory that contains dev.json."
    )


def _load_dev_json(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetImportError(f"Could not read dev.json at {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise DatasetImportError(
            f"Expected a JSON array of records in {path}, got {type(payload).__name__}."
        )
    return payload


def _safe_filename(db_id: str) -> str:
    return "".join(ch if ch in _SAFE_FILENAME_CHARS else "_" for ch in db_id)
