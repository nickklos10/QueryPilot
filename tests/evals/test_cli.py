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


def test_eval_replay_materializes_suite_from_jsonl(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    from querypilot import QueryPilot
    from querypilot.audit import AuditMetadata, JSONLAuditSink

    audit_path = tmp_path / "audit.jsonl"
    sink = JSONLAuditSink(audit_path)
    qp = QueryPilot.connect(
        database_url=f"sqlite:///{fixture_db_path}",
        dialect="sqlite",
        audit_sink=sink,
        audit_metadata=AuditMetadata(app_name="cli-test"),
    )
    qp.ask("Top customers by revenue")
    qp.ask("Count of customers")

    output_path = tmp_path / "replay.yaml"
    exit_code = cli_main(
        [
            "eval",
            "replay",
            "--audit-jsonl",
            str(audit_path),
            "--fixture-db",
            f"sqlite:///{fixture_db_path}",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert output_path.exists()
    captured = capsys.readouterr()
    assert "2 cases" in captured.out

    from querypilot.evals import load_suite

    suite = load_suite(output_path)
    assert len(suite.cases) == 2
    assert all(c.source == "audit_replay" for c in suite.cases)
    assert all("app:cli-test" in c.tags for c in suite.cases)


def test_eval_replay_writes_json_when_extension_is_json(
    tmp_path: Path, fixture_db_path: Path
) -> None:
    import json

    from querypilot import QueryPilot
    from querypilot.audit import JSONLAuditSink

    audit_path = tmp_path / "audit.jsonl"
    sink = JSONLAuditSink(audit_path)
    qp = QueryPilot.connect(
        database_url=f"sqlite:///{fixture_db_path}",
        dialect="sqlite",
        audit_sink=sink,
    )
    qp.ask("Top customers by revenue")

    output_path = tmp_path / "replay.json"
    exit_code = cli_main(
        [
            "eval",
            "replay",
            "--audit-jsonl",
            str(audit_path),
            "--fixture-db",
            f"sqlite:///{fixture_db_path}",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text())
    assert payload["name"] == "audit_replay"
    assert len(payload["cases"]) == 1


def test_eval_replay_missing_audit_file_exits(
    tmp_path: Path, fixture_db_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        cli_main(
            [
                "eval",
                "replay",
                "--audit-jsonl",
                str(tmp_path / "missing.jsonl"),
                "--fixture-db",
                f"sqlite:///{fixture_db_path}",
                "--output",
                str(tmp_path / "out.yaml"),
            ]
        )


def test_eval_check_passes_when_pass_rate_meets_threshold(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    suite = _smoke_suite(tmp_path, fixture_db_path)
    report_path = tmp_path / "report.json"
    cli_main(
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
    capsys.readouterr()  # discard run output

    exit_code = cli_main(
        [
            "eval",
            "check",
            "--report",
            str(report_path),
            "--threshold",
            "0.9",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "OK" in captured.out


def test_eval_check_exits_nonzero_on_threshold_violation(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    suite = _smoke_suite(tmp_path, fixture_db_path)
    report_path = tmp_path / "report.json"
    cli_main(
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
    capsys.readouterr()

    exit_code = cli_main(
        [
            "eval",
            "check",
            "--report",
            str(report_path),
            "--max-p95-ms",
            "0",  # current p95 will exceed this
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Regression detected" in captured.out
    assert "p95_latency_ms" in captured.out


def test_eval_check_writes_outcome_json(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    import json

    suite = _smoke_suite(tmp_path, fixture_db_path)
    report_path = tmp_path / "report.json"
    cli_main(
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
    capsys.readouterr()

    outcome_path = tmp_path / "outcome.json"
    cli_main(
        [
            "eval",
            "check",
            "--report",
            str(report_path),
            "--outcome-json",
            str(outcome_path),
        ]
    )

    payload = json.loads(outcome_path.read_text())
    assert payload["ok"] is True


def test_eval_check_with_baseline_detects_regression(
    tmp_path: Path, fixture_db_path: Path, capsys
) -> None:
    import json

    suite = _smoke_suite(tmp_path, fixture_db_path)
    report_path = tmp_path / "report.json"
    cli_main(
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
    capsys.readouterr()

    # Baseline was a perfect run; tamper the current report to simulate regression
    payload = json.loads(report_path.read_text())
    payload["pass_rate"] = 0.5
    payload["passed"] = 0
    payload["failed"] = payload["total_cases"]
    if payload["case_results"]:
        payload["case_results"][0]["passed"] = False
        payload["case_results"][0]["failure_category"] = "result_mismatch"
    broken_path = tmp_path / "broken.json"
    broken_path.write_text(json.dumps(payload))

    exit_code = cli_main(
        [
            "eval",
            "check",
            "--report",
            str(broken_path),
            "--baseline",
            str(report_path),
            "--threshold",
            "0.9",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Failed cases" in captured.out


def test_eval_init_scaffolds_directories(tmp_path: Path, capsys) -> None:
    exit_code = cli_main(["eval", "init", "--target", str(tmp_path)])

    assert exit_code == 0
    assert (tmp_path / "suites" / "smoke.yaml").exists()
    assert (tmp_path / "suites" / "safety.yaml").exists()
    assert (tmp_path / ".eval" / "README.md").exists()
    captured = capsys.readouterr()
    assert "Scaffolded 3 files" in captured.out


def test_eval_init_skips_existing_files_without_force(
    tmp_path: Path, capsys
) -> None:
    cli_main(["eval", "init", "--target", str(tmp_path)])
    capsys.readouterr()

    # Modify the smoke file then re-run without --force
    smoke = tmp_path / "suites" / "smoke.yaml"
    smoke.write_text("custom\n", encoding="utf-8")

    exit_code = cli_main(["eval", "init", "--target", str(tmp_path)])

    assert exit_code == 0
    assert smoke.read_text() == "custom\n"


def test_eval_init_force_overwrites(tmp_path: Path, capsys) -> None:
    cli_main(["eval", "init", "--target", str(tmp_path)])
    capsys.readouterr()
    smoke = tmp_path / "suites" / "smoke.yaml"
    smoke.write_text("custom\n", encoding="utf-8")

    cli_main(["eval", "init", "--target", str(tmp_path), "--force"])

    assert smoke.read_text() != "custom\n"
    assert "fixture_db" in smoke.read_text()


def test_eval_init_uses_placeholder_fixture_path(tmp_path: Path, capsys) -> None:
    cli_main(["eval", "init", "--target", str(tmp_path)])
    capsys.readouterr()

    smoke = (tmp_path / "suites" / "smoke.yaml").read_text()
    safety = (tmp_path / "suites" / "safety.yaml").read_text()

    # Scaffolded fixture path is an obvious placeholder so users know they
    # must edit it before running the suite.
    assert "REPLACE_ME" in smoke
    assert "REPLACE_ME" in safety
    # Comment guiding the user is included.
    assert "# Update fixture_db" in smoke


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
