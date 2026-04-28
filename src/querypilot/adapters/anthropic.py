from __future__ import annotations

from typing import Any


def anthropic_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "ask_database",
            "description": "Ask a natural-language question of the database using QueryPilot safety checks.",
            "input_schema": _object_schema(
                {"question": {"type": "string", "description": "Question to answer."}},
                ["question"],
            ),
        },
        {
            "name": "search_schema",
            "description": "Search tables and columns relevant to a natural-language query.",
            "input_schema": _object_schema(
                {"query": {"type": "string", "description": "Schema search query."}},
                ["query"],
            ),
        },
        {
            "name": "validate_sql",
            "description": "Validate SQL and return the safe rewritten form when allowed.",
            "input_schema": _object_schema(
                {"sql": {"type": "string", "description": "SQL to validate."}},
                ["sql"],
            ),
        },
        {
            "name": "execute_sql",
            "description": "Validate and execute read-only SQL safely.",
            "input_schema": _object_schema(
                {"sql": {"type": "string", "description": "SQL to execute."}},
                ["sql"],
            ),
        },
    ]


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
