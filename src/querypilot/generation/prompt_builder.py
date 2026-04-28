from __future__ import annotations

from querypilot.core.types import DatabaseSchema


def build_schema_context(schema: DatabaseSchema) -> str:
    lines: list[str] = []
    for table in schema.tables:
        columns = ", ".join(f"{column.name} {column.type}" for column in table.columns)
        lines.append(f"{table.name}({columns})")
    return "\n".join(lines)
