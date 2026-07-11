from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from querypilot import QueryPilot
from querypilot.evals.cost import (
    AnthropicCostTracker,
    LocalCostTracker,
    NullCostTracker,
    OpenAICostTracker,
)
from querypilot.evals.factory import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_LOCAL_MODEL,
    DEFAULT_OPENAI_MODEL,
    GENERATOR_NAMES,
    build_cost_tracker_factory,
    build_generator,
    build_qp_factory,
    load_suite_or_dir,
)
from querypilot.evals.suite import BenchmarkCase
from querypilot.generation.llm import OpenAICompatibleSQLGenerator
from querypilot.generation.sql_generator import DemoSQLGenerator


FIXTURE_DB = Path(__file__).resolve().parents[1] / "fixtures" / "demo.db"


def _install_fake_openai_module(monkeypatch) -> None:
    """Stub the `openai` import so factory tests run offline without the extra.

    CI installs only `.[dev,eval]` — the openai package is absent — and tests
    must never construct a real network client anyway.
    """

    class _FakeOpenAI:
        def __init__(self, base_url=None, api_key=None):
            self.base_url = base_url
            self.api_key = api_key

    module = types.ModuleType("openai")
    module.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", module)


def test_build_generator_demo_returns_demo_instance() -> None:
    generator = build_generator("demo")

    assert isinstance(generator, DemoSQLGenerator)


def test_build_generator_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown generator"):
        build_generator("notreal")


def test_build_generator_case_insensitive() -> None:
    generator = build_generator("DEMO")

    assert isinstance(generator, DemoSQLGenerator)


def test_build_cost_tracker_factory_demo_returns_null_factory() -> None:
    factory = build_cost_tracker_factory("demo")

    instance = factory()
    assert isinstance(instance, NullCostTracker)


def test_build_cost_tracker_factory_openai_returns_openai_class() -> None:
    factory = build_cost_tracker_factory("openai")

    instance = factory()
    assert isinstance(instance, OpenAICostTracker)


def test_build_cost_tracker_factory_anthropic_returns_anthropic_class() -> None:
    factory = build_cost_tracker_factory("anthropic")

    instance = factory()
    assert isinstance(instance, AnthropicCostTracker)


def test_build_cost_tracker_factory_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown generator"):
        build_cost_tracker_factory("notreal")


def test_build_generator_openai_compatible_from_flags(monkeypatch) -> None:
    _install_fake_openai_module(monkeypatch)

    generator = build_generator(
        "openai-compatible",
        model="qwen2.5-coder",
        base_url="http://localhost:8000/v1",
    )

    assert isinstance(generator, OpenAICompatibleSQLGenerator)
    assert generator.model == "qwen2.5-coder"
    assert "localhost:8000" in str(generator.client.base_url)


def test_build_generator_openai_compatible_defaults_model_and_base_url(
    monkeypatch,
) -> None:
    _install_fake_openai_module(monkeypatch)

    generator = build_generator("openai-compatible")

    assert isinstance(generator, OpenAICompatibleSQLGenerator)
    assert generator.model == DEFAULT_LOCAL_MODEL
    # base_url falls back to Ollama's default in the generator.
    assert "localhost:11434" in str(generator.client.base_url)


def test_build_cost_tracker_factory_openai_compatible_returns_local() -> None:
    factory = build_cost_tracker_factory("openai-compatible")

    assert isinstance(factory(), LocalCostTracker)


def test_build_qp_factory_uses_case_fixture_db_when_set() -> None:
    generator = build_generator("demo")
    factory = build_qp_factory(
        database_url="sqlite:///wrong.db",
        generator=generator,
    )
    case = BenchmarkCase(
        id="x",
        question="?",
        gold_sql="SELECT 1",
        fixture_db=f"sqlite:///{FIXTURE_DB}",
    )

    qp = factory(case)

    assert isinstance(qp, QueryPilot)
    assert qp.connector.database_url == f"sqlite:///{FIXTURE_DB}"


def test_build_qp_factory_falls_back_to_database_url() -> None:
    generator = build_generator("demo")
    factory = build_qp_factory(
        database_url=f"sqlite:///{FIXTURE_DB}",
        generator=generator,
    )
    case = BenchmarkCase(
        id="x",
        question="?",
        gold_sql="SELECT 1",
        fixture_db=f"sqlite:///{FIXTURE_DB}",
    )

    qp = factory(case)

    assert qp.connector.database_url == f"sqlite:///{FIXTURE_DB}"


def test_load_suite_or_dir_dispatches_on_path_kind(tmp_path: Path) -> None:
    file_payload = """
name: smoke
fixture_db: sqlite:///x.db
cases:
  - id: a
    question: "?"
    gold_sql: SELECT 1
"""
    suite_file = tmp_path / "smoke.yaml"
    suite_file.write_text(file_payload, encoding="utf-8")

    file_suite = load_suite_or_dir(suite_file)
    assert file_suite.name == "smoke"
    assert {c.id for c in file_suite.cases} == {"a"}

    suite_dir = tmp_path / "dir"
    suite_dir.mkdir()
    (suite_dir / "smoke.yaml").write_text(file_payload, encoding="utf-8")

    dir_suite = load_suite_or_dir(suite_dir)
    assert dir_suite.name == "smoke"
    assert {c.id for c in dir_suite.cases} == {"a"}


def test_generator_names_constant_lists_all_supported() -> None:
    assert GENERATOR_NAMES == ("demo", "openai", "anthropic", "openai-compatible")


def test_default_models_documented() -> None:
    assert DEFAULT_OPENAI_MODEL
    assert DEFAULT_ANTHROPIC_MODEL
    assert DEFAULT_LOCAL_MODEL
