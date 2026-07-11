from __future__ import annotations

from pathlib import Path
from typing import Callable

from querypilot import QueryPilot
from querypilot.evals.cost import (
    AnthropicCostTracker,
    CostTracker,
    LocalCostTracker,
    NullCostTracker,
    OpenAICostTracker,
)
from querypilot.evals.loader import load_suite, load_suite_dir
from querypilot.evals.pipeline import QueryPilotFactory
from querypilot.evals.suite import BenchmarkCase, BenchmarkSuite
from querypilot.generation.sql_generator import DemoSQLGenerator, SQLGenerator


GENERATOR_NAMES = ("demo", "openai", "anthropic", "openai-compatible")

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_LOCAL_MODEL = "llama3.1"


def build_generator(
    name: str, *, model: str | None = None, base_url: str | None = None
) -> SQLGenerator:
    normalized = name.lower()
    if normalized == "demo":
        return DemoSQLGenerator()
    if normalized == "openai":
        from querypilot.generation.llm import OpenAISQLGenerator

        return OpenAISQLGenerator(model=model or DEFAULT_OPENAI_MODEL)
    if normalized == "anthropic":
        from querypilot.generation.llm import AnthropicSQLGenerator

        return AnthropicSQLGenerator(model=model or DEFAULT_ANTHROPIC_MODEL)
    if normalized == "openai-compatible":
        from querypilot.generation.llm import OpenAICompatibleSQLGenerator

        return OpenAICompatibleSQLGenerator(
            model=model or DEFAULT_LOCAL_MODEL,
            base_url=base_url,
        )
    raise ValueError(
        f"Unknown generator {name!r}. Supported: {', '.join(GENERATOR_NAMES)}."
    )


def build_cost_tracker_factory(name: str) -> Callable[[], CostTracker]:
    normalized = name.lower()
    if normalized == "demo":
        return NullCostTracker
    if normalized == "openai":
        return OpenAICostTracker
    if normalized == "anthropic":
        return AnthropicCostTracker
    if normalized == "openai-compatible":
        return LocalCostTracker
    raise ValueError(
        f"Unknown generator {name!r}. Supported: {', '.join(GENERATOR_NAMES)}."
    )


def build_qp_factory(
    *,
    database_url: str,
    dialect: str = "sqlite",
    generator: SQLGenerator,
    max_rows: int = 100,
    timeout_seconds: int = 10,
    max_generation_attempts: int = 2,
) -> QueryPilotFactory:
    def _make(case: BenchmarkCase) -> QueryPilot:
        url = case.fixture_db or database_url
        case_dialect = case.fixture_dialect or dialect
        return QueryPilot.connect(
            database_url=url,
            dialect=case_dialect,
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
            max_generation_attempts=max_generation_attempts,
            generator=generator,
        )

    return _make


def load_suite_or_dir(path: str | Path) -> BenchmarkSuite:
    target = Path(path)
    if target.is_dir():
        return load_suite_dir(target)
    return load_suite(target)
