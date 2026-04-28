from __future__ import annotations

from pydantic import BaseModel

from querypilot.core.types import DatabaseSchema


class SQLGenerationPrompt(BaseModel):
    instructions: str
    user_prompt: str


def build_sql_generation_prompt(
    question: str,
    schema: DatabaseSchema,
    max_rows: int,
    validation_errors: list[str] | None = None,
    previous_sql: str | None = None,
) -> SQLGenerationPrompt:
    instructions = (
        "You generate safe SQL for QueryPilot. Return only JSON with keys "
        "`sql` and `explanation`. Generate SELECT-only SQL. Use only tables "
        "and columns present in the provided schema. Do not use mutations, DDL, "
        "multiple statements, comments, or vendor-specific unsafe commands. "
        f"The maximum row limit is {max_rows}; include a LIMIT no larger than that "
        "unless the query is an aggregate that returns one row."
    )
    if validation_errors:
        instructions += " Repair the SQL using the validation errors."

    parts = [
        f"Question: {question}",
        "",
        "Schema:",
        build_schema_context(schema),
    ]
    if previous_sql:
        parts.extend(["", f"Previous SQL: {previous_sql}"])
    if validation_errors:
        parts.extend(["", "Validation errors:", "\n".join(f"- {error}" for error in validation_errors)])
    return SQLGenerationPrompt(instructions=instructions, user_prompt="\n".join(parts))


def build_schema_context(schema: DatabaseSchema) -> str:
    lines: list[str] = []
    for table in schema.tables:
        columns = ", ".join(f"{column.name} {column.type}" for column in table.columns)
        lines.append(f"{table.name}({columns})")
    return "\n".join(lines)
