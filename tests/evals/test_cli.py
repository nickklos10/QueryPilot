from __future__ import annotations

import json
from pathlib import Path

import pytest

from querypilot.cli import main as cli_main


FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "demo.db"


@pytest.fixture(scope="module")
def fixture_db_path() -> Path:
    if not FIXTURE_DB.exists():
        from tests.fixtures.seed_demo import seed

        seed(FIXTURE_DB)
    return FIXTURE_DB


def _smoke_suite(tmp_path: Path, fixture_db_path: Path) -> Path:
    suite = tmp_path / "smoke.yaml"
    suite.write_text(
        f"""
name: cli_smoke
fixture_db: sqlite:///{fixture_db_path}
fixture_dialect: sqlite
thresholds:
  pass_rate: 0.99
cases:
  - id: count_customers
    question: "Count of customers"
    gold_sql: "SELECT COUNT(*) AS count FROM customers"
    expected_tables: [customers]
    tags: [smoke]
""",
        encoding="utf-8",
    )
    return suite


def test_eval_run_passes_with_demo_generator(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    suite = _smoke_suite(tmp_path, fixture_db_path)
    report_path = tmp_path / "report.json"

    exit_code = cli_main(
        [
            "eval",
            "run",
            "--suite",
            str(suite),
            "--database-url",
            f"sqlite:///{fixture_db_path}",
            "--generator",
            "demo",
            "--report",
            str(report_path),
            "--no-color",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "QueryPilot Eval Report" in captured.out
    assert "cli_smoke" in captured.out
    assert "Pass rate" in captured.out

    payload = json.loads(report_path.read_text())
    assert payload["pass_rate"] == 1.0
    assert payload["total_cases"] == 1


def test_eval_run_omitting_report_still_prints(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    suite = _smoke_suite(tmp_path, fixture_db_path)

    exit_code = cli_main(
        [
            "eval",
            "run",
            "--suite",
            str(suite),
            "--database-url",
            f"sqlite:///{fixture_db_path}",
            "--generator",
            "demo",
            "--no-color",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "QueryPilot Eval Report" in captured.out


def test_eval_run_missing_suite_file_raises_systemexit(
    tmp_path: Path, fixture_db_path: Path
) -> None:
    with pytest.raises(SystemExit, match="Failed to load suite"):
        cli_main(
            [
                "eval",
                "run",
                "--suite",
                str(tmp_path / "missing.yaml"),
                "--database-url",
                f"sqlite:///{fixture_db_path}",
                "--generator",
                "demo",
            ]
        )


def test_eval_run_invalid_generator_choice_exits(
    tmp_path: Path, fixture_db_path: Path
) -> None:
    suite = _smoke_suite(tmp_path, fixture_db_path)

    with pytest.raises(SystemExit):
        cli_main(
            [
                "eval",
                "run",
                "--suite",
                str(suite),
                "--database-url",
                f"sqlite:///{fixture_db_path}",
                "--generator",
                "definitely-not-a-real-generator",
            ]
        )


def test_eval_run_loads_suite_directory(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    suite_dir = tmp_path / "suites"
    suite_dir.mkdir()
    (suite_dir / "a.yaml").write_text(
        f"""
name: dir_smoke
fixture_db: sqlite:///{fixture_db_path}
cases:
  - id: count_a
    question: "Count of customers"
    gold_sql: "SELECT COUNT(*) AS count FROM customers"
""",
        encoding="utf-8",
    )
    (suite_dir / "b.yaml").write_text(
        f"""
name: dir_smoke_two
fixture_db: sqlite:///{fixture_db_path}
cases:
  - id: count_b
    question: "Count of customers"
    gold_sql: "SELECT COUNT(*) AS count FROM customers"
""",
        encoding="utf-8",
    )

    report_path = tmp_path / "report.json"
    exit_code = cli_main(
        [
            "eval",
            "run",
            "--suite",
            str(suite_dir),
            "--database-url",
            f"sqlite:///{fixture_db_path}",
            "--generator",
            "demo",
            "--report",
            str(report_path),
            "--no-color",
        ]
    )

    assert exit_code == 0
    payload = json.loads(report_path.read_text())
    assert payload["total_cases"] == 2


def test_eval_run_uses_suite_fixture_db_when_database_url_omitted(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    suite = _smoke_suite(tmp_path, fixture_db_path)

    exit_code = cli_main(
        [
            "eval",
            "run",
            "--suite",
            str(suite),
            "--generator",
            "demo",
            "--no-color",
        ]
    )

    assert exit_code == 0


def test_eval_run_no_color_strips_ansi(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    suite = _smoke_suite(tmp_path, fixture_db_path)

    cli_main(
        [
            "eval",
            "run",
            "--suite",
            str(suite),
            "--generator",
            "demo",
            "--no-color",
        ]
    )
    captured = capsys.readouterr()

    assert "\x1b[" not in captured.out
