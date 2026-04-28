from __future__ import annotations

import re

from querypilot.core.types import DatabaseSchema, SchemaMatch


def search_schema(schema: DatabaseSchema, query: str) -> list[SchemaMatch]:
    tokens = set(re.findall(r"[a-z0-9_]+", query.lower()))
    matches: list[SchemaMatch] = []
    for table in schema.tables:
        table_tokens = _name_tokens(table.name)
        table_score = len(tokens & table_tokens) * 3
        column_hits: list[str] = []
        for column in table.columns:
            column_score = len(tokens & _name_tokens(column.name))
            if column_score:
                column_hits.append(column.name)
                table_score += column_score
        if table_score:
            matches.append(
                SchemaMatch(
                    table=table.name,
                    column=", ".join(column_hits) if column_hits else None,
                    score=table_score,
                    reason="Matched table or column name",
                )
            )
    return sorted(matches, key=lambda match: (-match.score, match.table))


def _name_tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", name.lower()))
