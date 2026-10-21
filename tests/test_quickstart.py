from __future__ import annotations

import json
from pathlib import Path

import pytest

from querypilot.cli import main
from querypilot.demo import create_demo_db
from querypilot.project import (
    ProjectConfig,
    connect_project,
    load_project_config,
    resolve_generator_name,
)


@pytest.fixture
def demo_url(tmp_path: Path) -> str:
    return f"sqlite:///{create_demo_db(tmp_path / 'demo.db')}"


def test_demo_creates_db_and_runs_a_query(tmp_path: Path, capsys) -> None:
    target = tmp_path / "demo.db"

    assert main(["demo", "--path", str(target)]) == 0

    out = capsys.readouterr().out
    assert target.exists()
    assert "Acme Corp" in out
    assert "Blocked: Only SELECT queries are allowed." in out


def test_sql_command_runs_and_prints_table(demo_url: str, capsys) -> None:
    assert main(["sql", "--db", demo_url, "SELECT customer_name FROM customers ORDER BY id"]) == 0

    out = capsys.readouterr().out
    assert "customer_name" in out
    assert "Acme Corp" in out
    assert "(5 rows)" in out


def test_sql_command_reports_blocked_sql_with_exit_code(demo_url: str, capsys) -> None:
    assert main(["sql", "--db", demo_url, "DROP TABLE customers"]) == 1

    assert "Error:" in capsys.readouterr().err


def test_sql_validate_only_shows_rewritten_sql(demo_url: str, capsys) -> None:
    assert main(["sql", "--db", demo_url, "--validate-only", "SELECT id FROM customers"]) == 0

    assert "LIMIT" in capsys.readouterr().out


def test_schema_command_lists_and_describes_tables(demo_url: str, capsys) -> None:
    assert main(["schema", "--db", demo_url]) == 0
    assert "customers" in capsys.readouterr().out

    assert main(["schema", "--db", demo_url, "customers", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    columns = {column["name"] for column in payload["tables"][0]["columns"]}
    assert {"id", "customer_name", "arr"} <= columns


def test_ask_with_demo_generator(demo_url: str, capsys) -> None:
    code = main(["ask", "--db", demo_url, "--generator", "demo", "count invoices"])

    captured = capsys.readouterr()
    assert code == 0
    assert "SELECT COUNT(*)" in captured.out
    assert "offline demo generator" in captured.err


def test_init_writes_config_with_tables(demo_url: str, tmp_path: Path, capsys) -> None:
    target = tmp_path / "querypilot.yaml"

    assert main(["init", "--db", demo_url, "--path", str(target)]) == 0

    config = load_project_config(target)
    assert config.database_url == demo_url
    # Blank description placeholders are dropped on load.
    assert config.descriptions == {}
    assert "  customers: \"\"" in target.read_text()


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "querypilot.yaml"
    target.write_text("max_rows: 5\n")

    with pytest.raises(SystemExit):
        main(["init", "--path", str(target)])


def test_commands_read_project_config(demo_url: str, tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "querypilot.yaml").write_text(
        "database_url: ${DEMO_DB_URL}\n"
        "max_rows: 2\n"
        "hints: [\"ARR is annual recurring revenue in dollars\"]\n"
        "descriptions:\n"
        "  customers.arr: Annual recurring revenue\n"
        "  invoices: \"\"\n"
    )
    monkeypatch.setenv("DEMO_DB_URL", demo_url)
    monkeypatch.chdir(tmp_path)

    assert main(["sql", "SELECT customer_name FROM customers"]) == 0
    assert "(2 rows)" in capsys.readouterr().out


def test_project_config_rejects_missing_env_reference(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("QP_MISSING_URL", raising=False)
    path = tmp_path / "querypilot.yaml"
    path.write_text("database_url: ${QP_MISSING_URL}\n")

    with pytest.raises(ValueError, match="QP_MISSING_URL"):
        load_project_config(path)


def test_connect_project_applies_hints_and_descriptions(demo_url: str) -> None:
    config = ProjectConfig(
        database_url=demo_url,
        hints=["Paid invoices only"],
        descriptions={"customers.arr": "Annual recurring revenue"},
        generator={"provider": "demo"},
    )

    qp = connect_project(config)

    assert qp.config.hints == ["Paid invoices only"]
    arr = qp.get_schema().get_table("customers").get_column("arr")
    assert arr.comment == "Annual recurring revenue"


def test_resolve_generator_name_prefers_available_keys(monkeypatch) -> None:
    monkeypatch.delenv("QUERYPILOT_GENERATOR", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_generator_name("auto") == "demo"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert resolve_generator_name(None) == "anthropic"

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert resolve_generator_name("auto") == "openai"
    assert resolve_generator_name("demo") == "demo"


@pytest.mark.parametrize("client", ["claude-desktop", "cursor", "windsurf"])
def test_mcp_config_json_clients(client: str, capsys) -> None:
    assert main(["mcp-config", client, "--db", "postgresql://u@h/db"]) == 0

    payload = json.loads(capsys.readouterr().out)
    server = payload["mcpServers"]["querypilot"]
    assert server["args"][-2:] == ["querypilot", "mcp"]
    assert "querypilot[mcp]" in server["args"]
    assert server["env"]["QUERYPILOT_DATABASE_URL"] == "postgresql://u@h/db"


def test_mcp_config_vscode_and_claude_code(capsys) -> None:
    assert main(["mcp-config", "vscode", "--db", "duckdb:///a.duckdb", "--runner", "python"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["servers"]["querypilot"]["type"] == "stdio"
    assert payload["servers"]["querypilot"]["args"] == ["-m", "querypilot", "mcp"]

    assert main(["mcp-config", "claude-code", "--db", "postgresql://u@h/db"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("claude mcp add querypilot --env QUERYPILOT_DATABASE_URL=postgresql://u@h/db -- ")


def test_mcp_config_makes_relative_sqlite_paths_absolute(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["mcp-config", "cursor", "--db", "sqlite:///data/app.db"]) == 0

    url = json.loads(capsys.readouterr().out)["mcpServers"]["querypilot"]["env"]["QUERYPILOT_DATABASE_URL"]
    assert url == f"sqlite:///{(tmp_path / 'data' / 'app.db').resolve()}"


def test_from_config_reads_project_file(demo_url: str, tmp_path: Path, monkeypatch) -> None:
    from querypilot import QueryPilot

    (tmp_path / "querypilot.yaml").write_text(f"database_url: {demo_url}\nmax_rows: 3\nhints: [x]\n")
    monkeypatch.chdir(tmp_path)

    qp = QueryPilot.from_config(max_rows=4)

    assert qp.config.max_rows == 4
    assert qp.config.hints == ["x"]
    with pytest.raises(FileNotFoundError):
        QueryPilot.from_config(tmp_path / "missing.yaml")


def test_mcp_config_points_the_server_at_the_project_config(tmp_path: Path, monkeypatch, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "querypilot.yaml").write_text(
        "database_url: sqlite:///data/app.db\nblocked_tables: [payroll]\n"
    )
    monkeypatch.delenv("QUERYPILOT_CONFIG", raising=False)
    monkeypatch.delenv("QUERYPILOT_DATABASE_URL", raising=False)
    monkeypatch.chdir(project)

    # ./querypilot.yaml is picked up, and --db is not required.
    assert main(["mcp-config", "cursor"]) == 0
    env = json.loads(capsys.readouterr().out)["mcpServers"]["querypilot"]["env"]

    assert env["QUERYPILOT_CONFIG"] == str((project / "querypilot.yaml").resolve())
    # The config's relative SQLite path is resolved against the config's directory.
    assert env["QUERYPILOT_DATABASE_URL"] == f"sqlite:///{(project / 'data' / 'app.db').resolve()}"

    monkeypatch.chdir(tmp_path)
    assert main(["mcp-config", "claude-code", "--config", "project/querypilot.yaml", "--db", "postgresql://u@h/db"]) == 0
    out = capsys.readouterr().out
    assert f"--env QUERYPILOT_CONFIG={(project / 'querypilot.yaml').resolve()}" in out
    assert "--env QUERYPILOT_DATABASE_URL=postgresql://u@h/db" in out

    with pytest.raises(SystemExit, match="Config file not found"):
        main(["mcp-config", "cursor", "--config", "missing.yaml"])


def test_querypilot_config_env_is_honoured_from_another_directory(
    demo_url: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    config = tmp_path / "project" / "querypilot.yaml"
    config.parent.mkdir()
    config.write_text(f"database_url: {demo_url}\nmax_rows: 1\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("QUERYPILOT_DATABASE_URL", raising=False)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("QUERYPILOT_CONFIG", str(config))

    assert main(["sql", "SELECT customer_name FROM customers"]) == 0
    assert "(1 row)" in capsys.readouterr().out

    monkeypatch.setenv("QUERYPILOT_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(SystemExit, match="Config file not found"):
        main(["sql", "SELECT 1"])
