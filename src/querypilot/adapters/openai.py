from __future__ import annotations

from typing import Any


def openai_tools() -> list[dict[str, Any]]:
    return [
        _function_tool(
            "ask_database",
            "Ask a natural-language question of the database using QueryPilot safety checks.",
            {"question": {"type": "string", "description": "Question to answer."}},
            ["question"],
        ),
        _function_tool(
            "search_schema",
            "Search tables and columns relevant to a natural-language query.",
            {"query": {"type": "string", "description": "Schema search query."}},
            ["query"],
        ),
        _function_tool(
            "validate_sql",
            "Validate SQL and return the safe rewritten form when allowed.",
            {"sql": {"type": "string", "description": "SQL to validate."}},
            ["sql"],
        ),
        _function_tool(
            "execute_sql",
            "Validate and execute read-only SQL safely.",
            {"sql": {"type": "string", "description": "SQL to execute."}},
            ["sql"],
        ),
    ]


def _function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }
