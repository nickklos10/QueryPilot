"""Choose the schema context a model sees for one question.

Two steps, both deterministic:

1. ``visible_schema`` removes what the access policy hides: blocked tables,
   tables outside ``allowed_tables``, blocked columns and columns outside
   ``allowed_columns``. Hidden objects never reach a prompt, a "did you mean"
   hint, or a sample query.
2. ``prune_schema`` keeps the full schema whenever it fits the token budget
   (pruning a schema that fits costs 4.8-11.6 EX in published ablations) and
   only above the budget keeps the most relevant tables plus the foreign-key
   bridge tables needed to join them.
"""
from __future__ import annotations

from collections import deque

from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import DatabaseSchema, TableSchema
from querypilot.generation.prompt_builder import render_table
from querypilot.schema.sampling import (
    column_is_visible,
    sample_token_allowance,
    table_is_visible,
)
from querypilot.schema.search import table_scores

__all__ = [
    "column_is_visible",
    "estimate_tokens",
    "foreign_key_graph",
    "prune_schema",
    "schema_tokens",
    "shortest_path",
    "table_is_visible",
    "table_tokens",
    "visible_schema",
]

# Matched tables considered when adding join bridges (bounds the pairwise BFS).
_MAX_BRIDGED_TABLES = 15


def visible_schema(schema: DatabaseSchema, config: QueryPilotConfig) -> DatabaseSchema:
    """Copy of ``schema`` without the tables and columns the access policy hides."""
    tables: list[TableSchema] = []
    for table in schema.tables:
        if not table_is_visible(table.name, config):
            continue
        columns = [
            column.model_copy()
            for column in table.columns
            if column_is_visible(table.name, column.name, config)
        ]
        kept = {column.name.lower() for column in columns}
        foreign_keys = [
            foreign_key.model_copy()
            for foreign_key in table.foreign_keys
            if table_is_visible(foreign_key.referred_table, config)
            and all(name.lower() in kept for name in foreign_key.columns)
            and all(
                column_is_visible(foreign_key.referred_table, name, config)
                for name in foreign_key.referred_columns
            )
        ]
        tables.append(
            table.model_copy(update={"columns": columns, "foreign_keys": foreign_keys})
        )
    return schema.model_copy(update={"tables": tables})


def estimate_tokens(text: str) -> int:
    """Rough token count (about four characters per token)."""
    return (len(text) + 3) // 4


def table_tokens(table: TableSchema, dialect: str, sample_values: int = 0) -> int:
    """Estimated prompt tokens for one rendered table, plus room for its samples."""
    return (
        estimate_tokens(render_table(table, dialect))
        + 1
        + sample_token_allowance(table, sample_values if not _has_samples(table) else 0)
    )


def _has_samples(table: TableSchema) -> bool:
    return any(column.sample_values for column in table.columns)


def schema_tokens(schema: DatabaseSchema, sample_values: int = 0) -> int:
    return sum(table_tokens(table, schema.dialect, sample_values) for table in schema.tables)


def prune_schema(
    schema: DatabaseSchema,
    question: str,
    token_budget: int,
    sample_values: int = 0,
) -> DatabaseSchema:
    """Keep the whole schema if it fits ``token_budget``; otherwise the relevant part.

    Tables are ranked by keyword relevance to the question (then by being a
    foreign-key neighbour of a relevant table, then by schema order) and added
    while they fit the budget; the top-ranked table is always kept. Tables on
    the shortest foreign-key path between any two relevant tables are then
    added even past the budget, so every kept table stays joinable. A budget
    of 0 or less disables pruning. ``sample_values`` reserves room for the
    sample values that will be added to text columns after pruning.
    """
    if token_budget <= 0 or not schema.tables:
        return schema
    sizes = {
        table.name.lower(): table_tokens(table, schema.dialect, sample_values)
        for table in schema.tables
    }
    if sum(sizes.values()) <= token_budget:
        return schema

    graph = foreign_key_graph(schema)
    scores = table_scores(schema, question)
    order = {table.name.lower(): index for index, table in enumerate(schema.tables)}
    matched = sorted(scores, key=lambda name: (-scores[name], order[name]))
    neighbours = {
        neighbour
        for name in matched
        for neighbour in graph.get(name, ())
        if neighbour not in scores
    }

    def rank(name: str) -> tuple[int, int, int]:
        tier = 0 if name in scores else 1 if name in neighbours else 2
        return (tier, -scores.get(name, 0), order[name])

    chosen: list[str] = []
    used = 0
    for name in sorted(order, key=rank):
        if not chosen or used + sizes[name] <= token_budget:
            chosen.append(name)
            used += sizes[name]

    chosen_set = set(chosen)
    anchors = [name for name in matched if name in chosen_set][:_MAX_BRIDGED_TABLES]
    for index, start in enumerate(anchors):
        for goal in anchors[index + 1 :]:
            path = shortest_path(graph, start, goal)
            if path:
                chosen_set.update(path)

    return schema.model_copy(
        update={"tables": [t for t in schema.tables if t.name.lower() in chosen_set]}
    )


def foreign_key_graph(schema: DatabaseSchema) -> dict[str, set[str]]:
    """Undirected adjacency between tables (lower-cased names) linked by foreign keys."""
    names = schema.table_names
    graph: dict[str, set[str]] = {name: set() for name in names}
    for table in schema.tables:
        source = table.name.lower()
        for foreign_key in table.foreign_keys:
            target = foreign_key.referred_table.split(".")[-1].lower()
            if target in names and target != source:
                graph[source].add(target)
                graph[target].add(source)
    return graph


def shortest_path(graph: dict[str, set[str]], start: str, goal: str) -> list[str]:
    """Tables on the shortest foreign-key path from ``start`` to ``goal`` (inclusive)."""
    if start == goal:
        return [start]
    previous: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbour in sorted(graph.get(current, ())):
            if neighbour in previous:
                continue
            previous[neighbour] = current
            if neighbour == goal:
                path = [goal]
                step = previous[goal]
                while step is not None:
                    path.append(step)
                    step = previous[step]
                return path[::-1]
            queue.append(neighbour)
    return []
