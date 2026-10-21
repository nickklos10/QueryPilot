"""Run a SaaSPulse suite with the local `claude` CLI as the model behind AnthropicSQLGenerator.

Useful for comparing two QueryPilot checkouts without an API key: the CLI uses
the signed-in Claude subscription. PYTHONPATH selects the tree being measured.

    PYTHONPATH=<tree>/src python benchmarks/run_with_claude_cli.py <tree> report.json \
        [--suite benchmarks/saaspulse_natural/natural.yaml] [--model haiku] [--workers 6]

For per-database suites from `querypilot eval import` (Spider/BIRD), pass the
output directory with --each; every suite runs on its own fixture and the
pass rate is aggregated. Raise --max-rows so the LIMIT cap never truncates a
result the gold query returns in full.

Each call runs `claude -p` from an empty directory with no tools, settings,
hooks or MCP servers, so only QueryPilot's system and user prompts reach the model.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

EMPTY_DIR = Path(tempfile.mkdtemp(prefix="querypilot-claude-cli-"))


class ClaudeCLIMessages:
    calls = 0

    def create(self, *, model, max_tokens, system, messages):
        ClaudeCLIMessages.calls += 1
        user = messages[-1]["content"]
        command = [
            "claude", "-p", "--model", model, "--system-prompt", system,
            "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
            "--setting-sources", "", "--no-session-persistence", "--output-format", "json",
        ]
        last_error = ""
        for attempt in range(4):
            proc = subprocess.run(command, check=False, input=user, cwd=EMPTY_DIR, capture_output=True, text=True, timeout=300)
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                last_error = proc.stdout[-300:] + proc.stderr[-300:]
                time.sleep(2 * (attempt + 1))
                continue
            if payload.get("is_error"):
                last_error = str(payload.get("result"))
                time.sleep(2 * (attempt + 1))
                continue
            usage = payload.get("usage", {})
            return SimpleNamespace(
                content=[SimpleNamespace(text=payload["result"])],
                usage=SimpleNamespace(
                    input_tokens=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                ),
            )
        raise RuntimeError(f"claude CLI failed: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tree")
    parser.add_argument("report")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=None)
    parser.add_argument("--suite", default=None)
    parser.add_argument("--each", action="store_true", help="Run every *.yaml in --suite separately.")
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=None)
    args = parser.parse_args()

    import querypilot
    from querypilot.evals.factory import build_qp_factory, load_suite_or_dir
    from querypilot.evals.report import write_json
    from querypilot.evals.suite_runner import run_suite
    from querypilot.generation.llm import AnthropicSQLGenerator

    print("querypilot from", Path(querypilot.__file__).parent, file=sys.stderr)
    tree = Path(args.tree)
    generator = AnthropicSQLGenerator(client=SimpleNamespace(messages=ClaudeCLIMessages()), model=args.model)

    def run(suite):
        factory_kwargs = {"database_url": suite.fixture_db or "", "generator": generator}
        for key, value in (
            ("max_generation_attempts", args.attempts),
            ("max_rows", args.max_rows),
            ("timeout_seconds", args.timeout_seconds),
        ):
            if value is not None:
                factory_kwargs[key] = value
        return run_suite(
            suite,
            qp_factory=build_qp_factory(**factory_kwargs),
            max_workers=args.workers,
            generator_name="anthropic (claude CLI)",
            model_name=args.model,
        )

    if args.each:
        from querypilot.evals.loader import load_suite

        cases = []
        for path in sorted(Path(args.suite).glob("*.yaml")):
            data = json.loads(run(load_suite(path)).model_dump_json())
            passed = sum(case["passed"] for case in data["case_results"])
            print(f"{path.stem}: {passed}/{len(data['case_results'])}", file=sys.stderr, flush=True)
            cases.extend({**case, "suite": path.stem} for case in data["case_results"])
        Path(args.report).write_text(json.dumps({"case_results": cases}, default=str))
        passed = sum(case["passed"] for case in cases)
        print(json.dumps({"passed": passed, "total": len(cases), "rate": passed / len(cases),
                          "llm_calls": ClaudeCLIMessages.calls}, indent=2))
        return 0

    report = run(load_suite_or_dir(args.suite or tree / "benchmarks/saaspulse/correctness.yaml"))
    write_json(report, args.report)
    data = json.loads(Path(args.report).read_text())
    summary = {key: data.get(key) for key in (
        "pass_rate", "correctness_rate", "first_pass_rate", "repair_rate", "avg_repair_attempts",
        "p95_latency_ms", "prompt_tokens", "completion_tokens",
    )}
    summary["llm_calls"] = ClaudeCLIMessages.calls
    summary["failure_breakdown"] = data.get("failure_breakdown")
    summary["tags"] = {
        tag: f"{roll.get('passed')}/{roll.get('total')}"
        for tag, roll in (data.get("tag_rollups") or {}).items()
        if tag in {"easy", "medium", "hard"}
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
