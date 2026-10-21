from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from querypilot.cli import main
from querypilot.evals.replay import replay_from_jsonl
from querypilot.trace import classify
from querypilot.trace.doctor import Redactor, diagnose, render_markdown, replay_records
from querypilot.trace.guard import guard_sql
from querypilot.trace.hooks import (
    HookOptions,
    install_hooks,
    main_hook,
    run_hook,
    tool_matcher,
    uninstall_hooks,
)
from querypilot.trace.schema_cache import (
    CachedColumn,
    CachedDataset,
    SchemaCache,
    parse_foundry_schema,
)
from querypilot.trace.tools import (
    FOUNDRY_SCHEMA_TOOL,
    FOUNDRY_SQL_TOOL,
    ToolRegistry,
    parse_tool_spec,
)
from querypilot.trace.transcript import QUERY, SCHEMA, load_calls, result_text

FIXTURE = Path(__file__).parent / "fixtures" / "claude_code_foundry_transcript.jsonl"
RID = "ri.foundry.main.dataset.11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUERYPILOT_HOME", str(tmp_path / "qp-home"))


def _usage_cache() -> SchemaCache:
    return SchemaCache(
        {
            RID: CachedDataset(
                key=RID,
                path="/Demo/Usage/monthly_usage",
                columns=[
                    CachedColumn("Customer_Name", "STRING"),
                    CachedColumn("Product_Name", "STRING"),
                    CachedColumn("Report_Date", "DATE"),
                    CachedColumn("MAU", "LONG"),
                ],
            )
        }
    )


# --------------------------------------------------------------------------- classify


@pytest.mark.parametrize(
    ("text", "is_error", "status", "error_class"),
    [
        ("|a|\n|---|\n|1|\n", False, classify.OK, None),
        (
            "Sql Query Failed\nError Message: Failed to execute SQL query: [UNRESOLVED_COLUMN.WITH_SUGGESTION] x",
            False,
            classify.ERROR,
            classify.UNRESOLVED_COLUMN,
        ),
        ("Sql Query Failed\nError Message: [DATATYPE_MISMATCH.BINARY_OP_DIFF_TYPES] y", False, "error", "type_mismatch"),
        ("Error: result (1 characters) exceeds maximum allowed tokens.", False, "error", "result_too_large"),
        ("Error: fetch failed\n{}", True, "error", "upstream"),
        ("Error: Failed to fetch 500 \n**Suggestion:** A 403 Forbidden ... RID is incorrect", True, "error", "upstream"),
        ("Error: Failed to fetch 403 \n**Suggestion:** dataset RID is incorrect", True, "error", "unknown_table"),
        ('Error: Failed to fetch 400 \n{"errorName":"QueryParseError"}', True, "error", "syntax"),
        ("Error: Failed to fetch 400 . Error: Failed to fetch 400\nat Object.fetch (cli.cjs:1:2)", True, "error", "upstream"),
        ("Sql Query Failed\nError Message: [MISSING_GROUP_BY] z", False, "error", "sql_analysis"),
        ("The user doesn't want to proceed with this tool use.", True, classify.DENIED, None),
        ("QueryPilot guard blocked this query before it ran:\n- x", True, classify.BLOCKED, None),
    ],
)
def test_classify_output(text, is_error, status, error_class) -> None:
    outcome = classify.classify_output(text, is_error=is_error)
    assert outcome.status == status
    assert outcome.error_class == error_class


def test_result_rows_containing_error_words_are_still_success() -> None:
    text = "|status|note|\n|---|---|\n|Error|Sql Query Failed upstream|\n|[ACTIVE]|timeout|\n"
    outcome = classify.classify_output(text)
    assert outcome.status == classify.OK
    assert outcome.row_count == 2
    assert classify.markdown_header(text) == ["status", "note"]


# --------------------------------------------------------------------------- tools


def test_tool_specs_map_any_tool_by_name_and_field() -> None:
    spec = parse_tool_spec("mcp__warehouse__*=query:snowflake:max_rows")
    registry = ToolRegistry.from_args(["mcp__warehouse__*=query:snowflake:max_rows"])
    assert (spec.sql_field, spec.dialect, spec.row_limit_field) == ("query", "snowflake", "max_rows")
    assert registry.query_spec("mcp__warehouse__run_query") == spec
    assert registry.query_spec(FOUNDRY_SQL_TOOL).sql_field == "sqlQuery"
    assert registry.schema_spec(FOUNDRY_SCHEMA_TOOL) is not None
    assert registry.query_spec("Bash") is None
    with pytest.raises(ValueError):
        parse_tool_spec("no-field")


def test_tool_matcher_uses_exact_lists_when_possible() -> None:
    assert tool_matcher([FOUNDRY_SQL_TOOL, FOUNDRY_SCHEMA_TOOL]) == f"{FOUNDRY_SQL_TOOL}|{FOUNDRY_SCHEMA_TOOL}"
    assert tool_matcher(["mcp__wh__*", "mcp__a.b__run"]) == r"^(?:mcp__wh__.*|mcp__a\.b__run)$"


# --------------------------------------------------------------------------- transcripts


def test_load_calls_pairs_tool_use_with_result() -> None:
    calls = load_calls([FIXTURE])
    queries = [c for c in calls if c.kind == QUERY]
    schemas = [c for c in calls if c.kind == SCHEMA]

    assert len(queries) == 8
    assert len(schemas) == 1 and schemas[0].dataset.key == RID
    by_id = {c.call_id: c for c in calls}
    first = by_id["toolu_02"]
    assert first.status == "error" and first.error_class == "unresolved_column"
    assert first.latency_s == 4.0
    assert first.params == {"rowLimit": 100}
    assert first.question == "Which products had the most monthly active users last month?"
    assert by_id["toolu_05"].row_count == 2
    assert by_id["toolu_07"].error_class == "result_too_large" and by_id["toolu_07"].latency_s == 100.0
    assert by_id["toolu_08"].error_class == "upstream"
    assert by_id["toolu_10"].status == "no_result"
    assert by_id["toolu_06"].question == "Show me some raw rows."


def test_resumed_transcripts_are_deduplicated(tmp_path) -> None:
    copy = tmp_path / "resumed.jsonl"
    copy.write_text(FIXTURE.read_text())
    assert len(load_calls([FIXTURE, copy])) == len(load_calls([FIXTURE]))


def test_result_text_handles_mcp_shapes() -> None:
    assert result_text("plain") == "plain"
    assert result_text([{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert result_text(None) == ""


# --------------------------------------------------------------------------- schema cache


def test_schema_cache_round_trip_and_select_star(tmp_path) -> None:
    calls = load_calls([FIXTURE])
    schema_call = next(c for c in calls if c.kind == SCHEMA)
    assert [c.name for c in schema_call.dataset.columns] == ["Customer_Name", "Product_Name", "Report_Date", "MAU"]

    cache = SchemaCache()
    learned = cache.learn_from_select_star("SELECT * FROM `/Demo/other` LIMIT 5", ["a", "b"], "spark")
    assert learned is not None and cache.lookup("/demo/OTHER").columns[1].name == "b"
    assert cache.learn_from_select_star("SELECT a FROM `/Demo/other`", ["a"], "spark") is None
    assert cache.learn_from_select_star("SELECT * FROM x JOIN y ON x.id = y.id", ["id"], "spark") is None

    cache.add(schema_call.dataset)
    path = cache.save(tmp_path / "cache.json")
    loaded = SchemaCache.load(path)
    assert loaded.lookup(RID).path == "/Demo/Usage/monthly_usage"
    assert loaded.lookup("/Demo/Usage/monthly_usage").key == RID
    assert loaded.evict([RID]) == [RID] and loaded.lookup("/Demo/Usage/monthly_usage") is None


def test_select_star_never_overwrites_schema_tool_entry() -> None:
    cache = _usage_cache()
    assert cache.learn_from_select_star(f"SELECT * FROM `{RID}`", ["only_one"], "spark") is None
    assert len(cache.lookup(RID).columns) == 4


def test_parse_foundry_schema_rejects_non_schema_text() -> None:
    assert parse_foundry_schema("no json here") is None
    assert parse_foundry_schema('{"properties": {}}') is None


# --------------------------------------------------------------------------- guard


def test_guard_denies_unknown_column_with_did_you_mean() -> None:
    decision = guard_sql(f"SELECT Product, SUM(MAU) FROM `{RID}` GROUP BY Product", _usage_cache())
    assert decision.denied and decision.rule == "schema" and decision.checked_columns
    assert "did you mean monthly_usage.Product_Name" in decision.reason
    assert f"monthly_usage = `{RID}`" in decision.reason


def test_guard_allows_valid_sql_and_uncached_datasets() -> None:
    cache = _usage_cache()
    sql = (
        f"WITH t AS (SELECT Product_Name AS p, MAU FROM `{RID}`) "
        "SELECT p, SUM(MAU) AS m FROM t GROUP BY p ORDER BY m DESC LIMIT 5"
    )
    assert not guard_sql(sql, cache).denied
    other = guard_sql("SELECT nope FROM `ri.foundry.main.dataset.99999999-2222-3333-4444-555555555555`", cache)
    assert not other.denied and not other.checked_columns
    assert not guard_sql("SELECT FROM WHERE (((", cache).denied


def test_guard_require_schema_and_malformed_rid() -> None:
    cache = _usage_cache()
    unknown = "ri.foundry.main.dataset.99999999-2222-3333-4444-555555555555"
    decision = guard_sql(f"SELECT a FROM `{unknown}`", cache, require_schema=True)
    assert decision.denied and decision.rule == "schema_required" and "Fetch the schema" in decision.reason
    bad = guard_sql("SELECT a FROM `ri.foundry.main.dataset.11111111-2222`", cache)
    assert bad.denied and "not a valid dataset RID" in bad.reason
    pinned = "ri.foundry.main.transaction.0000-1@master.ri.foundry.main.dataset.11111111-2222-3333-4444-555555555555"
    assert not guard_sql(f"SELECT * FROM `{pinned}`", cache).denied


def test_guard_epoch_milliseconds_on_date_columns() -> None:
    cache = _usage_cache()
    for sql in (
        f"SELECT Report_Date / 1000 FROM `{RID}`",
        f"SELECT MAU FROM `{RID}` WHERE Report_Date >= 1790812800000",
        f"SELECT MAU FROM `{RID}` WHERE Report_Date >= unix_timestamp('2026-06-01') * 1000",
    ):
        decision = guard_sql(sql, cache)
        assert decision.denied, sql
        assert "not epoch milliseconds" in decision.reason
    assert not guard_sql(f"SELECT MAU / 1000 FROM `{RID}` WHERE Report_Date >= DATE '2026-01-01'", cache).denied


def test_guard_row_cap() -> None:
    cache = _usage_cache()
    decision = guard_sql(f"SELECT * FROM `{RID}`", cache, max_rows=200)
    assert decision.denied and decision.rule == "limit"
    assert decision.limited_sql.endswith("LIMIT 200")
    assert guard_sql(f"SELECT * FROM `{RID}` LIMIT 5000", cache, max_rows=200).denied
    assert not guard_sql(f"SELECT * FROM `{RID}` LIMIT 50", cache, max_rows=200).denied
    assert not guard_sql(f"SELECT * FROM `{RID}`", cache, max_rows=200, tool_row_limit=100).denied
    assert not guard_sql(f"SELECT COUNT(*), MAX(MAU) FROM `{RID}`", cache, max_rows=200).denied


def test_guard_ignores_struct_fields_and_dotted_names() -> None:
    cache = _usage_cache()
    struct = (
        f"WITH z AS (SELECT explode(arrays_zip(Product_Name, MAU)) zz FROM `{RID}`) "
        "SELECT zz.Product_Name FROM z"
    )
    assert not guard_sql(struct, cache).denied
    assert not guard_sql(f"SELECT `vendor.entityid` FROM `{RID}`", cache).denied


# --------------------------------------------------------------------------- doctor


def test_diagnose_sample_transcript() -> None:
    report = diagnose(load_calls([FIXTURE]))

    assert (report.calls, report.ran, report.failed, report.no_result) == (8, 7, 4, 1)
    assert report.failure_rate == pytest.approx(4 / 7)
    assert {row.error_class: row.count for row in report.classes} == {
        "unresolved_column": 1,
        "type_mismatch": 1,
        "result_too_large": 1,
        "upstream": 1,
    }
    assert {row.error_class: row.fix for row in report.classes}["type_mismatch"] == "data"
    assert report.chains.chains == 2 and report.chains.recovered == 2
    assert report.chains.schema_fetched_in_chain == 1
    assert report.latency.stalls == 1 and report.latency.p50 == 3.0
    assert report.guard.caught == {"type_mismatch": 1}
    assert report.guard.false_blocks == 0
    fixes = {f.title: f.fix for f in report.findings}
    assert fixes["Date columns read as epoch milliseconds"] == "data"
    assert fixes["Validate columns before the query runs (QueryPilot guard)"] == "tool"
    assert any("epoch" in hint for hint in report.hints)
    assert any(key.endswith(".Report_Date") for key in report.descriptions)


def test_markdown_report_is_redacted() -> None:
    text = render_markdown(diagnose(load_calls([FIXTURE])))
    assert "## Failures by class" in text and "| Unresolved column | 1 |" in text
    assert RID not in text and "/Demo/Usage" not in text and "monthly_usage" not in text
    assert "Acme" not in text and "Globex" not in text


def test_redactor_replaces_literals_and_datasets() -> None:
    redactor = Redactor(True)
    out = redactor.sql(f"SELECT a FROM `{RID}` WHERE name = 'Alice'")
    assert "Alice" not in out and RID not in out and "dataset_A" in out
    assert redactor.text(f"`{RID}` failed for '/Org/secret/path'") == "`dataset_A` failed for '?'"


def test_replay_export_feeds_eval_replay(tmp_path) -> None:
    records = replay_records(load_calls([FIXTURE]))
    assert [r.question for r in records] == [
        "Which products had the most monthly active users last month?",
        "Show me some raw rows.",
    ]
    assert "DATE '2026-09-01'" in records[0].rewritten_sql
    assert records[0].details["failure_classes"] == ["unresolved_column", "type_mismatch"]

    path = tmp_path / "replay.jsonl"
    path.write_text("".join(r.model_dump_json() + "\n" for r in records))
    suite = replay_from_jsonl(path, fixture_db="sqlite:///mirror.db")
    assert len(suite.cases) == 2
    assert "app:trace-unresolved_column" in suite.cases[0].tags


# --------------------------------------------------------------------------- hooks


def test_install_and_uninstall_preserve_other_hooks(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    settings.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [other]}}))
    options = HookOptions(audit_log=tmp_path / "audit.jsonl", guard=True, max_rows=200)

    install_hooks(settings, options, python="/usr/bin/python3")
    install_hooks(settings, options, python="/usr/bin/python3")  # idempotent
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == other and len(pre) == 2
    assert pre[1]["matcher"] == FOUNDRY_SQL_TOOL
    command = pre[1]["hooks"][0]["command"]
    assert command.startswith("/usr/bin/python3 -m querypilot hooks run pre")
    assert "--guard" in command and "--max-rows 200" in command
    assert FOUNDRY_SCHEMA_TOOL in data["hooks"]["PostToolUse"][0]["matcher"]
    assert "PostToolUseFailure" in data["hooks"]

    assert uninstall_hooks(settings) == 3
    data = json.loads(settings.read_text())
    assert data["hooks"] == {"PreToolUse": [other]}


def test_logging_only_install_has_no_pre_hook(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    install_hooks(settings, HookOptions(audit_log=tmp_path / "a.jsonl"))
    assert set(json.loads(settings.read_text())["hooks"]) == {"PostToolUse", "PostToolUseFailure"}


def _hook_payloads(path: Path):
    """Turn a transcript into the PreToolUse/PostToolUse payloads Claude Code would send."""
    uses = {}
    for line in path.read_text().splitlines():
        record = json.loads(line)
        content = record["message"]["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            base = {"session_id": record["sessionId"], "transcript_path": str(path)}
            if block.get("type") == "tool_use":
                uses[block["id"]] = block
                yield "pre", {**base, "hook_event_name": "PreToolUse", "tool_name": block["name"],
                              "tool_input": block["input"], "tool_use_id": block["id"]}
            elif block.get("type") == "tool_result":
                use = uses[block["tool_use_id"]]
                payload = {**base, "tool_name": use["name"], "tool_input": use["input"],
                           "tool_use_id": use["id"], "duration_ms": 1500}
                if block.get("is_error"):
                    yield "post-failure", {**payload, "hook_event_name": "PostToolUseFailure",
                                           "error": result_text(block["content"])}
                else:
                    yield "post", {**payload, "hook_event_name": "PostToolUse", "tool_response": block["content"]}


def test_hooks_replaying_sample_transcript_log_every_query_call(tmp_path) -> None:
    options = HookOptions(audit_log=tmp_path / "audit.jsonl", cache_path=tmp_path / "cache.json")
    outputs = [run_hook(event, payload, options) for event, payload in _hook_payloads(FIXTURE)]

    assert all(out is None for out in outputs)  # logging only: never changes a call
    hooked = {c.call_id: c for c in load_calls([options.audit_log])}
    direct = {c.call_id: c for c in load_calls([FIXTURE]) if c.status != "no_result"}
    assert set(hooked) == set(direct)
    for call_id, call in direct.items():
        assert (hooked[call_id].status, hooked[call_id].error_class) == (call.status, call.error_class)
        assert hooked[call_id].latency_s == 1.5
    assert SchemaCache.load(options.cache_path).lookup(RID) is not None

    report = diagnose(list(hooked.values()))
    assert report.failed == 4 and report.chains.recovered == 2
    lines = options.audit_log.read_text()
    assert "Alpha" not in lines and "Acme" not in lines  # result rows are never logged


def test_guard_hook_denies_then_allows_identical_resend(tmp_path) -> None:
    options = HookOptions(audit_log=tmp_path / "audit.jsonl", cache_path=tmp_path / "cache.json", guard=True)
    _usage_cache().save(options.cache_path)
    payload = {
        "session_id": "s1",
        "tool_name": FOUNDRY_SQL_TOOL,
        "tool_use_id": "t1",
        "tool_input": {"sqlQuery": f"SELECT Product FROM `{RID}` LIMIT 5", "rowLimit": 5},
    }
    first = run_hook("pre", payload, options)
    decision = first["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "Product_Name" in decision["permissionDecisionReason"]
    assert "send it again unchanged" in decision["permissionDecisionReason"]
    blocked = load_calls([options.audit_log])
    assert [c.status for c in blocked] == ["blocked"]

    assert run_hook("pre", {**payload, "tool_use_id": "t2"}, options) is None
    # It succeeded, so the cached schema was stale: the post hook evicts it.
    run_hook("post", {**payload, "tool_use_id": "t2", "tool_response": [{"type": "text", "text": "|Product|\n|---|\n|A|\n"}]}, options)
    assert SchemaCache.load(options.cache_path).lookup(RID) is None


def test_guard_hook_annotates_epoch_rendered_dates(tmp_path) -> None:
    options = HookOptions(audit_log=tmp_path / "audit.jsonl", cache_path=tmp_path / "cache.json", guard=True)
    _usage_cache().save(options.cache_path)
    out = run_hook(
        "post",
        {
            "session_id": "s1",
            "tool_name": FOUNDRY_SQL_TOOL,
            "tool_use_id": "t1",
            "tool_input": {"sqlQuery": f"SELECT Product_Name, Report_Date FROM `{RID}` LIMIT 2"},
            "tool_response": [{"type": "text", "text": "|Product_Name|Report_Date|\n|---|---|\n|A|1788220800000|\n"}],
        },
        options,
    )
    assert "Report_Date (DATE)" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_ignores_other_tools_and_never_raises(tmp_path) -> None:
    options = HookOptions(audit_log=tmp_path / "audit.jsonl", guard=True)
    assert run_hook("pre", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, options) is None
    out = io.StringIO()
    assert main_hook("post", options, stdin=io.StringIO("not json"), stdout=out) == 0
    assert out.getvalue() == ""
    assert "Traceback" in (tmp_path / "hook-errors.log").read_text()


def test_hook_subprocess_round_trip(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    cache = tmp_path / "cache.json"
    _usage_cache().save(cache)
    payload = {
        "session_id": "s1",
        "tool_name": FOUNDRY_SQL_TOOL,
        "tool_use_id": "t1",
        "tool_input": {"sqlQuery": f"SELECT Report_Date / 1000 FROM `{RID}` LIMIT 1"},
    }
    proc = subprocess.run(
        [sys.executable, "-m", "querypilot", "hooks", "run", "pre", "--guard",
         "--audit-log", str(audit), "--cache", str(cache)],
        input=json.dumps(payload), capture_output=True, text=True, check=True, timeout=60,
    )
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "not epoch milliseconds" in out["hookSpecificOutput"]["permissionDecisionReason"]


# --------------------------------------------------------------------------- CLI


def test_cli_trace_import_and_doctor(tmp_path, capsys) -> None:
    audit = tmp_path / "audit.jsonl"
    cache = tmp_path / "cache.json"
    assert main(["trace", "import", str(FIXTURE), "--output", str(audit), "--cache", str(cache)]) == 0
    assert main(["trace", "import", str(FIXTURE), "--output", str(audit), "--cache", str(cache)]) == 0
    out = capsys.readouterr().out
    assert "wrote 9 new records" in out and "wrote 0 new records" in out
    assert SchemaCache.load(cache).lookup(RID) is not None
    assert "Alpha" not in audit.read_text()

    report = tmp_path / "report.md"
    suite = tmp_path / "suite.yaml"
    code = main(
        ["trace", "doctor", str(audit), "--output", str(report), "--suite", str(suite),
         "--fixture-db", "sqlite:///mirror.db"]
    )
    assert code == 0
    assert "| Data type mismatch | 1 |" in report.read_text()
    assert "Wrote 2 cases" in capsys.readouterr().out
    assert suite.is_file()


def test_cli_hooks_install_dry_run(tmp_path, capsys) -> None:
    settings = tmp_path / "settings.json"
    assert main(["hooks", "install", "--settings", str(settings), "--guard", "--dry-run"]) == 0
    assert not settings.exists()
    assert "querypilot hooks run pre" in capsys.readouterr().out
    assert main(["hooks", "install", "--settings", str(settings), "--tool", "mcp__wh__run=query:snowflake"]) == 0
    assert "mcp__wh__run" in settings.read_text()
    assert main(["hooks", "uninstall", "--settings", str(settings)]) == 0


def test_guard_text_keeps_column_names_and_drops_dataset_names() -> None:
    redactor = Redactor(True)
    text = "Column 'product' does not exist; did you mean usage.Product_Name, other.Product_Name?"
    assert redactor.guard_text(text) == "Column 'product' does not exist; did you mean Product_Name?"
