from __future__ import annotations

from typing import Any


def openai_tools() -> list[dict[str, Any]]:
    """Generic Chat Completions definitions; prefer ``qp.as_openai_tools()``, which knows the database."""
    from querypilot.adapters.toolkit import default_tool_definitions

    return [
        openai_tool(name, description, parameters)
        for name, description, parameters in default_tool_definitions()
    ]


def openai_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """A Chat Completions function tool."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
            "strict": True,
        },
    }


def openai_responses_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """A Responses API function tool (flat, not nested under ``function``)."""
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": parameters,
        "strict": True,
    }
