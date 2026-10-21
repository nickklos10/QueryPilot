from __future__ import annotations

from typing import Any


def anthropic_tools() -> list[dict[str, Any]]:
    """Generic Messages API definitions; prefer ``qp.as_anthropic_tools()``, which knows the database."""
    from querypilot.adapters.toolkit import default_tool_definitions

    return [
        anthropic_tool(name, description, parameters)
        for name, description, parameters in default_tool_definitions()
    ]


def anthropic_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """An Anthropic Messages API tool."""
    return {"name": name, "description": description, "input_schema": parameters}
