"""A local cache of dataset schemas, learned from the agent's own tool calls.

Entries come from schema tool results (``get_foundry_dataset_schema``) and from
successful ``SELECT *`` queries on a single dataset, whose result header lists
every column. The guard validates SQL against this cache, so it only judges
queries whose datasets it has seen.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import sqlglot
from sqlglot import exp

FOUNDRY_RID = re.compile(r"ri\.foundry\.main\.dataset\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

SOURCE_SCHEMA_TOOL = "schema_tool"
SOURCE_SELECT_STAR = "select_star"


def querypilot_home() -> Path:
    return Path(os.getenv("QUERYPILOT_HOME") or Path.home() / ".querypilot")


def default_cache_path() -> Path:
    return querypilot_home() / "trace" / "schema-cache.json"


@dataclass
class CachedColumn:
    name: str
    type: str = "UNKNOWN"
    description: str = ""


@dataclass
class CachedDataset:
    key: str
    path: str | None = None
    columns: list[CachedColumn] = field(default_factory=list)
    source: str = SOURCE_SCHEMA_TOOL
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def display_name(self) -> str:
        if self.path:
            return self.path.rstrip("/").rsplit("/", 1)[-1] or self.key
        return self.key


class SchemaCache:
    def __init__(self, datasets: dict[str, CachedDataset] | None = None) -> None:
        self.datasets: dict[str, CachedDataset] = dict(datasets or {})
        self._paths = {d.path.lower(): d.key for d in self.datasets.values() if d.path}

    def __len__(self) -> int:
        return len(self.datasets)

    def lookup(self, ref: str) -> CachedDataset | None:
        ref = ref.strip().strip("`\"")
        hit = self.datasets.get(ref)
        if hit is None and ref.lower() in self._paths:
            hit = self.datasets.get(self._paths[ref.lower()])
        return hit

    def add(self, dataset: CachedDataset) -> bool:
        """Store a dataset; a schema-tool entry is never replaced by a SELECT * one."""
        current = self.datasets.get(dataset.key)
        if (
            current is not None
            and current.source == SOURCE_SCHEMA_TOOL
            and dataset.source != SOURCE_SCHEMA_TOOL
        ):
            return False
        if current is not None and dataset.path is None:
            dataset.path = current.path
        self.datasets[dataset.key] = dataset
        if dataset.path:
            self._paths[dataset.path.lower()] = dataset.key
        return True

    def evict(self, refs: list[str]) -> list[str]:
        """Forget datasets whose cached schema proved wrong (e.g. rebuilt with new columns)."""
        evicted = []
        for ref in refs:
            dataset = self.lookup(ref)
            if dataset is None:
                continue
            self.datasets.pop(dataset.key, None)
            if dataset.path:
                self._paths.pop(dataset.path.lower(), None)
            evicted.append(dataset.key)
        return evicted

    def learn_from_schema_result(self, text: str, tool_input: dict | None = None) -> CachedDataset | None:
        dataset = parse_foundry_schema(text)
        if dataset is None:
            return None
        if dataset.path is None and tool_input and tool_input.get("datasetPath"):
            dataset.path = str(tool_input["datasetPath"])
        self.add(dataset)
        return dataset

    def learn_from_select_star(self, sql: str, header: list[str] | None, dialect: str) -> CachedDataset | None:
        """``SELECT * FROM one_dataset`` returns every column; record their names."""
        if not header:
            return None
        ref = select_star_source(sql, dialect)
        if ref is None:
            return None
        current = self.lookup(ref)
        dataset = CachedDataset(
            key=current.key if current else ref,
            path=current.path if current else (ref if ref.startswith("/") else None),
            columns=[CachedColumn(name) for name in header if name],
            source=SOURCE_SELECT_STAR,
        )
        return dataset if self.add(dataset) else None

    # ------------------------------------------------------------------ storage

    @classmethod
    def load(cls, path: str | Path | None = None) -> SchemaCache:
        target = Path(path) if path else default_cache_path()
        if not target.is_file():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        datasets = {}
        for key, raw in (payload.get("datasets") or {}).items():
            columns = [CachedColumn(**column) for column in raw.get("columns", [])]
            datasets[key] = CachedDataset(
                key=key,
                path=raw.get("path"),
                columns=columns,
                source=raw.get("source", SOURCE_SCHEMA_TOOL),
                updated_at=raw.get("updated_at", ""),
            )
        return cls(datasets)

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else default_cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "datasets": {
                key: {k: v for k, v in asdict(dataset).items() if k != "key"}
                for key, dataset in sorted(self.datasets.items())
            },
        }
        # Hooks for parallel tool calls can save at once; replace atomically.
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".schema-cache-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
        os.replace(tmp, target)
        return target


def parse_foundry_schema(text: str) -> CachedDataset | None:
    """Parse a ``get_foundry_dataset_schema`` result (a JSON resource tree)."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        payload = json.loads(text[start:])
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    properties = payload.get("properties") or {}
    rid = properties.get("rid")
    if not rid:
        return None
    columns: list[CachedColumn] = []
    for child in payload.get("children") or []:
        if not isinstance(child, dict) or child.get("type") != "schema":
            continue
        for column in child.get("children") or []:
            props = column.get("properties") if isinstance(column, dict) else None
            if props and props.get("name"):
                columns.append(
                    CachedColumn(
                        name=str(props["name"]),
                        type=str(props.get("type") or "UNKNOWN"),
                        description=str(props.get("description") or ""),
                    )
                )
    if not columns:
        return None
    return CachedDataset(key=str(rid), path=properties.get("path"), columns=columns)


def table_refs(expression: exp.Expression) -> list[exp.Table]:
    """Physical table references, excluding CTE names."""
    ctes = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    refs = []
    for table in expression.find_all(exp.Table):
        if not table.name:
            continue
        if not table.args.get("db") and table.name.lower() in ctes:
            continue
        refs.append(table)
    return refs


def table_ref_name(table: exp.Table) -> str:
    parts = [part.name for part in table.parts]
    return ".".join(parts)


def select_star_source(sql: str, dialect: str) -> str | None:
    try:
        expression = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        return None
    if not isinstance(expression, exp.Select) or expression.args.get("joins"):
        return None
    if len(expression.expressions) != 1 or not isinstance(expression.expressions[0], exp.Star):
        return None
    source = expression.args.get("from") or expression.args.get("from_")
    table = source.this if source is not None else None
    if not isinstance(table, exp.Table) or expression.find(exp.CTE) is not None:
        return None
    return table_ref_name(table)
