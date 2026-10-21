"""Project config file (``querypilot.yaml``) shared by the CLI, servers and library."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from querypilot.access import AccessPolicy

CONFIG_FILENAMES = ("querypilot.yaml", "querypilot.yml", "querypilot.json")

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class GeneratorSettings(BaseModel):
    # auto picks openai or anthropic from the API keys in the environment, else demo.
    provider: str = "auto"
    model: str | None = None
    base_url: str | None = None


class ProjectConfig(BaseModel):
    database_url: str | None = None
    dialect: str | None = None
    max_rows: int = Field(default=100, ge=1)
    timeout_seconds: int = Field(default=10, ge=1)
    allowed_tables: list[str] | None = None
    blocked_tables: list[str] | None = None
    schemas: list[str] | None = None
    hints: list[str] = Field(default_factory=list)
    descriptions: dict[str, str] = Field(default_factory=dict)
    sample_values: int = Field(default=3, ge=0)
    schema_token_budget: int = Field(default=6000, ge=0)
    access_policy: AccessPolicy | None = None
    generator: GeneratorSettings = Field(default_factory=GeneratorSettings)


def find_config(start: str | Path | None = None) -> Path | None:
    """Return the first querypilot config file in ``start`` (default: the current directory)."""
    directory = Path(start) if start is not None else Path.cwd()
    for name in CONFIG_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def load_project_config(path: str | Path) -> ProjectConfig:
    """Load a project config; ``${VAR}`` references are expanded from the environment."""
    config_path = Path(path)
    text = config_path.read_text()
    if config_path.suffix == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise ImportError('Install PyYAML (pip install "querypilot[eval]") to read YAML config.') from exc
        payload = yaml.safe_load(text)
    config = ProjectConfig.model_validate(_expand_env(payload or {}))
    # Blank entries are placeholders from `querypilot init`, not real descriptions.
    config.descriptions = {key: value for key, value in config.descriptions.items() if value and value.strip()}
    return config


def resolve_generator_name(provider: str | None) -> str:
    """Map "auto" (or nothing) to a concrete generator based on the environment."""
    name = (provider or os.getenv("QUERYPILOT_GENERATOR") or "auto").lower()
    if name != "auto":
        return name
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "demo"


def build_generator(settings: GeneratorSettings) -> Any:
    from querypilot.evals.factory import build_generator as build_named_generator

    name = resolve_generator_name(settings.provider)
    return build_named_generator(name, model=settings.model, base_url=settings.base_url)


def connect_project(config: ProjectConfig, **overrides: Any):
    """Build a QueryPilot client from a ProjectConfig; keyword overrides win."""
    from querypilot.core.client import QueryPilot

    kwargs: dict[str, Any] = {
        "database_url": config.database_url,
        "dialect": config.dialect,
        "max_rows": config.max_rows,
        "timeout_seconds": config.timeout_seconds,
        "allowed_tables": config.allowed_tables,
        "blocked_tables": config.blocked_tables,
        "access_policy": config.access_policy,
        "schemas": config.schemas,
        "hints": config.hints,
        "descriptions": config.descriptions,
        "sample_values": config.sample_values,
        "schema_token_budget": config.schema_token_budget,
    }
    kwargs.update({key: value for key, value in overrides.items() if value is not None})
    if not kwargs.get("database_url"):
        raise ValueError("No database URL: set database_url in querypilot.yaml, pass --db, or set QUERYPILOT_DATABASE_URL.")
    if "generator" not in kwargs:
        kwargs["generator"] = build_generator(config.generator)
    return QueryPilot.connect(**kwargs)


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} referenced in the QueryPilot config is not set.")
        return os.environ[name]

    return _ENV_REFERENCE.sub(replace, value)
