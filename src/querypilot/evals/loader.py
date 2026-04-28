from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from querypilot.evals.suite import BenchmarkCase, BenchmarkSuite


class SuiteLoadError(ValueError):
    """Raised when a benchmark suite cannot be loaded or parsed."""


_SQLITE_PREFIX = "sqlite:///"


def load_suite(path: str | Path) -> BenchmarkSuite:
    suite_path = Path(path).resolve()
    if not suite_path.is_file():
        raise SuiteLoadError(f"Suite file not found: {suite_path}")

    payload = _parse_file(suite_path)
    if not isinstance(payload, dict):
        raise SuiteLoadError(f"Suite file must contain a mapping at the top level: {suite_path}")

    suite = _build_suite(payload, suite_path.parent)
    return suite


def load_suite_dir(path: str | Path) -> BenchmarkSuite:
    suite_dir = Path(path).resolve()
    if not suite_dir.is_dir():
        raise SuiteLoadError(f"Suite directory not found: {suite_dir}")

    suite_files = sorted(
        p for p in suite_dir.iterdir() if p.suffix.lower() in {".yaml", ".yml", ".json"}
    )
    if not suite_files:
        raise SuiteLoadError(f"No .yaml/.yml/.json suite files found in: {suite_dir}")

    sub_suites = [load_suite(suite_file) for suite_file in suite_files]
    head, *rest = sub_suites
    head_path = suite_files[0]

    for sub, sub_path in zip(rest, suite_files[1:]):
        _require_dir_match(head, head_path, sub, sub_path, "fixture_db")
        _require_dir_match(head, head_path, sub, sub_path, "fixture_dialect")
        _require_dir_match(head, head_path, sub, sub_path, "thresholds")
        _require_dir_match(head, head_path, sub, sub_path, "comparison")

    merged_cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    for sub in sub_suites:
        for case in sub.cases:
            if case.id in seen_ids:
                raise SuiteLoadError(
                    f"Duplicate case id across suite directory {suite_dir}: {case.id!r}"
                )
            seen_ids.add(case.id)
            merged_cases.append(case)

    return BenchmarkSuite(
        name=head.name,
        fixture_db=head.fixture_db,
        fixture_dialect=head.fixture_dialect,
        thresholds=head.thresholds,
        comparison=head.comparison,
        cases=merged_cases,
    )


def _require_dir_match(
    head: BenchmarkSuite,
    head_path: Path,
    sub: BenchmarkSuite,
    sub_path: Path,
    field: str,
) -> None:
    head_value = getattr(head, field)
    sub_value = getattr(sub, field)
    if head_value != sub_value:
        raise SuiteLoadError(
            f"{field!r} differs across suite directory: "
            f"{head_path.name} has {head_value!r} but {sub_path.name} has {sub_value!r}. "
            "All suite files in a directory must share fixture_db, fixture_dialect, "
            "thresholds, and comparison settings."
        )


def _parse_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SuiteLoadError(f"Invalid JSON in {path}: {exc}") from exc

    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise SuiteLoadError(
                "PyYAML is required to load YAML suites. Install with `pip install querypilot[eval]`."
            ) from exc
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SuiteLoadError(f"Invalid YAML in {path}: {exc}") from exc

    raise SuiteLoadError(f"Unsupported suite file extension: {path.suffix} ({path})")


def _build_suite(payload: dict[str, Any], suite_dir: Path) -> BenchmarkSuite:
    payload = dict(payload)

    suite_fixture_db = payload.get("fixture_db")
    if suite_fixture_db is not None:
        payload["fixture_db"] = _resolve_fixture_db(str(suite_fixture_db), suite_dir)

    raw_cases = payload.get("cases") or []
    if not isinstance(raw_cases, list):
        raise SuiteLoadError("Suite 'cases' must be a list.")

    resolved_cases: list[dict[str, Any]] = []
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise SuiteLoadError(f"Each case must be a mapping; got {type(raw).__name__}.")
        case = dict(raw)
        case_fixture_db = case.get("fixture_db")
        if case_fixture_db is not None:
            case["fixture_db"] = _resolve_fixture_db(str(case_fixture_db), suite_dir)
        resolved_cases.append(case)
    payload["cases"] = resolved_cases

    try:
        return BenchmarkSuite.model_validate(payload)
    except ValueError as exc:
        raise SuiteLoadError(f"Invalid suite definition: {exc}") from exc


def _resolve_fixture_db(fixture_db: str, suite_dir: Path) -> str:
    if not fixture_db.startswith(_SQLITE_PREFIX):
        return fixture_db

    raw_path = fixture_db[len(_SQLITE_PREFIX):]
    if not raw_path or raw_path.startswith(":memory:"):
        return fixture_db

    candidate = Path(raw_path)
    if candidate.is_absolute():
        return fixture_db

    resolved = (suite_dir / candidate).resolve()
    return f"{_SQLITE_PREFIX}{resolved}"
