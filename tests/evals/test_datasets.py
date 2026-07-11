from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from querypilot.evals import (
    BenchmarkSuite,
    ImportResult,
    detect_format,
    import_dataset,
    load_suite,
    run_suite,
)
from querypilot.evals.datasets import DatasetImportError
from querypilot.evals.factory import build_qp_factory
from querypilot.evals.loader import SuiteLoadError, load_suite_dir
from querypilot.generation.sql_generator import DemoSQLGenerator


# --------------------------------------------------------------------------- #
# Synthetic dataset builders (tiny; generated at runtime, nothing committed).
# --------------------------------------------------------------------------- #


def _make_sqlite(path: Path, script: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


def _build_spider(root: Path, *, include_missing_db: bool = False) -> Path:
    """Spider layout: dev.json + database/<db_id>/<db_id>.sqlite."""
    _make_sqlite(
        root / "database" / "concert_singer" / "concert_singer.sqlite",
        """
        CREATE TABLE singer (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
        INSERT INTO singer VALUES (1, 'Ana', 30), (2, 'Ben', 25), (3, 'Cara', 41);
        """,
    )
    _make_sqlite(
        root / "database" / "pets_1" / "pets_1.sqlite",
        """
        CREATE TABLE pets (id INTEGER PRIMARY KEY, pet_type TEXT, weight REAL);
        INSERT INTO pets VALUES (1, 'cat', 4.2), (2, 'dog', 12.0);
        """,
    )

    records = [
        {
            "db_id": "concert_singer",
            "question": "How many singers are there?",
            "query": "SELECT count(*) FROM singer",
        },
        {
            "db_id": "concert_singer",
            "question": "List singer names by age descending",
            "query": "SELECT name FROM singer ORDER BY age DESC",
        },
        {
            "db_id": "pets_1",
            "question": "How many pets are there?",
            "query": "SELECT count(*) FROM pets",
        },
        {
            "db_id": "concert_singer",
            "question": "This one has dirty gold SQL",
            "query": "SELECT nonexistent_col FROM singer",
        },
    ]
    if include_missing_db:
        records.append(
            {
                "db_id": "ghost_db",  # no sqlite file exists for this db_id
                "question": "Points at a fixture that was never downloaded",
                "query": "SELECT 1",
            }
        )
    (root / "dev.json").write_text(json.dumps(records), encoding="utf-8")
    return root


def _build_bird(root: Path) -> Path:
    """BIRD layout: dev.json (SQL + evidence) + dev_databases/<db_id>/<db_id>.sqlite."""
    _make_sqlite(
        root / "dev_databases" / "financial" / "financial.sqlite",
        """
        CREATE TABLE account (
            account_id INTEGER PRIMARY KEY,
            district_id INTEGER,
            frequency TEXT
        );
        INSERT INTO account VALUES (1, 10, 'monthly'), (2, 20, 'weekly'), (3, 10, 'monthly');
        """,
    )
    records = [
        {
            "db_id": "financial",
            "question": "How many accounts are there?",
            "SQL": "SELECT count(*) FROM account",
            "evidence": "an account is one row in the account table",
        },
        {
            "db_id": "financial",
            "question": "List the district ids",
            "SQL": "SELECT district_id FROM account",
            "evidence": "",
        },
    ]
    (root / "dev.json").write_text(json.dumps(records), encoding="utf-8")
    return root


@pytest.fixture()
def spider_dir(tmp_path: Path) -> Path:
    return _build_spider(tmp_path / "spider")


@pytest.fixture()
def bird_dir(tmp_path: Path) -> Path:
    return _build_bird(tmp_path / "bird")


# --------------------------------------------------------------------------- #
# Format detection
# --------------------------------------------------------------------------- #


def test_detect_format_spider(spider_dir: Path) -> None:
    assert detect_format(spider_dir) == "spider"


def test_detect_format_bird(bird_dir: Path) -> None:
    assert detect_format(bird_dir) == "bird"


def test_detect_format_falls_back_to_record_keys(tmp_path: Path) -> None:
    # No database/ or dev_databases/ subdir; detection must read the records.
    root = tmp_path / "loose"
    root.mkdir()
    (root / "dev.json").write_text(
        json.dumps([{"db_id": "x", "question": "q", "SQL": "SELECT 1"}]),
        encoding="utf-8",
    )
    assert detect_format(root) == "bird"


# --------------------------------------------------------------------------- #
# Happy-path imports
# --------------------------------------------------------------------------- #


def test_import_spider_happy_path(spider_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = import_dataset(spider_dir, out)

    assert isinstance(result, ImportResult)
    assert result.dataset_format == "spider"
    assert result.total_records == 4
    assert result.imported_cases == 3  # the dirty-gold record is skipped
    assert result.skipped_bad_gold == 1
    assert result.db_count == 2

    suite = load_suite(out / "spider_dev_concert_singer.yaml")
    assert isinstance(suite, BenchmarkSuite)
    assert suite.name == "spider_dev_concert_singer"
    assert suite.fixture_db.startswith("sqlite:///")
    assert suite.fixture_db.endswith("concert_singer.sqlite")
    assert suite.fixture_dialect == "sqlite"

    ids = {c.id for c in suite.cases}
    assert ids == {"spider_dev_0000", "spider_dev_0001"}
    case = suite.cases[0]
    assert case.gold_sql == "SELECT count(*) FROM singer"
    assert "spider" in case.tags
    assert "concert_singer" in case.tags


def test_import_bird_folds_evidence_into_question(bird_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = import_dataset(bird_dir, out)

    assert result.dataset_format == "bird"
    assert result.imported_cases == 2
    assert result.db_count == 1

    suite = load_suite(out / "bird_dev_financial.yaml")
    with_evidence = next(c for c in suite.cases if c.id == "bird_dev_0000")
    assert "Evidence: an account is one row" in with_evidence.question
    assert with_evidence.notes == "an account is one row in the account table"
    assert "bird" in with_evidence.tags
    assert "financial" in with_evidence.tags

    # Blank evidence is left off entirely.
    no_evidence = next(c for c in suite.cases if c.id == "bird_dev_0001")
    assert "Evidence:" not in no_evidence.question
    assert no_evidence.notes is None


# --------------------------------------------------------------------------- #
# Multi-db output shape
# --------------------------------------------------------------------------- #


def test_multi_db_writes_one_suite_per_db(spider_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = import_dataset(spider_dir, out)

    names = sorted(p.name for p in result.written_suites)
    assert names == ["spider_dev_concert_singer.yaml", "spider_dev_pets_1.yaml"]

    # Each per-db suite binds to its own fixture_db.
    concert = load_suite(out / "spider_dev_concert_singer.yaml")
    pets = load_suite(out / "spider_dev_pets_1.yaml")
    assert concert.fixture_db != pets.fixture_db
    assert pets.fixture_db.endswith("pets_1.sqlite")
    assert {c.id for c in pets.cases} == {"spider_dev_0002"}  # global, stable id


def test_output_dir_is_not_a_single_directory_suite(spider_dir: Path, tmp_path: Path) -> None:
    # The per-db files deliberately carry differing fixture_db values, so the
    # output directory is NOT loadable as one directory-suite (by design).
    out = tmp_path / "out"
    import_dataset(spider_dir, out)
    with pytest.raises(SuiteLoadError, match="fixture_db"):
        load_suite_dir(out)


# --------------------------------------------------------------------------- #
# Filtering: --limit and --db
# --------------------------------------------------------------------------- #


def test_limit_caps_imported_cases(spider_dir: Path, tmp_path: Path) -> None:
    result = import_dataset(spider_dir, tmp_path / "out", limit=1)
    assert result.imported_cases == 1
    assert result.db_count == 1


def test_db_filter_selects_single_db_with_stable_ids(spider_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = import_dataset(spider_dir, out, db_ids=["pets_1"])

    assert result.imported_cases == 1
    assert result.db_count == 1
    suite = load_suite(out / "spider_dev_pets_1.yaml")
    # Id reflects the record's ORIGINAL position in dev.json (index 2),
    # unaffected by the db filter.
    assert {c.id for c in suite.cases} == {"spider_dev_0002"}


# --------------------------------------------------------------------------- #
# Bad gold + missing fixture handling (warn/skip vs --strict)
# --------------------------------------------------------------------------- #


def test_bad_gold_is_skipped_by_default(spider_dir: Path, tmp_path: Path) -> None:
    result = import_dataset(spider_dir, tmp_path / "out")
    assert result.skipped_bad_gold == 1
    assert any("failed to execute" in w for w in result.warnings)


def test_bad_gold_errors_under_strict(spider_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(DatasetImportError, match="failed to execute"):
        import_dataset(spider_dir, tmp_path / "out", strict=True)


def test_missing_fixture_is_skipped_by_default(tmp_path: Path) -> None:
    root = _build_spider(tmp_path / "spider", include_missing_db=True)
    result = import_dataset(root, tmp_path / "out")
    # The dirty-gold record + the ghost-db record are both dropped.
    assert result.skipped_missing_fixture == 1
    assert result.imported_cases == 3
    assert any("ghost_db" in w for w in result.warnings)


def test_missing_fixture_errors_under_strict(tmp_path: Path) -> None:
    root = _build_spider(tmp_path / "spider", include_missing_db=True)
    # Filter to just the ghost db so strict trips on the missing fixture first.
    with pytest.raises(DatasetImportError, match="fixture SQLite not found"):
        import_dataset(root, tmp_path / "out", db_ids=["ghost_db"], strict=True)


# --------------------------------------------------------------------------- #
# End-to-end: an imported suite runs through run_suite without crashing.
# --------------------------------------------------------------------------- #


def test_imported_suite_runs_end_to_end(spider_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    import_dataset(spider_dir, out)
    suite = load_suite(out / "spider_dev_concert_singer.yaml")

    qp_factory = build_qp_factory(
        database_url=suite.fixture_db,
        generator=DemoSQLGenerator(),
    )
    report = run_suite(suite, qp_factory=qp_factory)

    # The harness must complete regardless of whether the demo generator's
    # SQL matches gold.
    assert report.total_cases == len(suite.cases)
    assert report.passed + report.failed == report.total_cases


# --------------------------------------------------------------------------- #
# Error surfaces
# --------------------------------------------------------------------------- #


def test_missing_dataset_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(DatasetImportError, match="Dataset directory not found"):
        import_dataset(tmp_path / "nope", tmp_path / "out")


def test_missing_dev_json_raises(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    (root / "database").mkdir(parents=True)  # looks like Spider, but no dev.json
    with pytest.raises(DatasetImportError, match="No dev.json"):
        import_dataset(root, tmp_path / "out")


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_eval_import(spider_dir: Path, tmp_path: Path, capsys) -> None:
    from querypilot.cli import main as cli_main

    out = tmp_path / "out"
    exit_code = cli_main(
        [
            "eval",
            "import",
            "--dataset",
            str(spider_dir),
            "--output",
            str(out),
            "--format",
            "spider",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Imported 3 spider cases into 2 per-db suites" in captured.out
    assert "Skipped 1 cases (gold SQL did not execute)" in captured.out
    assert (out / "spider_dev_concert_singer.yaml").exists()
    assert (out / "spider_dev_pets_1.yaml").exists()


def test_cli_eval_import_strict_exits(tmp_path: Path) -> None:
    from querypilot.cli import main as cli_main

    root = _build_spider(tmp_path / "spider")
    with pytest.raises(SystemExit, match="Import failed"):
        cli_main(
            [
                "eval",
                "import",
                "--dataset",
                str(root),
                "--output",
                str(tmp_path / "out"),
                "--strict",
            ]
        )
