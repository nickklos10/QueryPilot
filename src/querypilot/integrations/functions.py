"""QueryPilot tools as plain, typed Python functions.

Most agent frameworks build a tool from a function's name, type hints and docstring
(Pydantic AI, smolagents, CrewAI ``@tool``, Google ADK, AutoGen, ...), so these work
anywhere without an adapter::

    from pydantic_ai import Agent
    agent = Agent("openai:gpt-5", tools=as_functions(qp))
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, create_model

from querypilot.adapters.toolkit import QueryPilotTool, QueryPilotToolkit, to_json

if TYPE_CHECKING:
    from querypilot.core.client import QueryPilot


def as_functions(
    querypilot: QueryPilot | QueryPilotToolkit, **toolkit_options: Any
) -> list[Callable[..., dict[str, Any]]]:
    """Return one typed function per tool. Each returns a dict and never raises."""
    toolkit = resolve_toolkit(querypilot, **toolkit_options)
    return [tool_function(toolkit, tool) for tool in toolkit.tools]


def resolve_toolkit(
    querypilot: QueryPilot | QueryPilotToolkit, **toolkit_options: Any
) -> QueryPilotToolkit:
    if isinstance(querypilot, QueryPilotToolkit):
        if toolkit_options:
            raise TypeError("Toolkit options cannot be combined with an existing QueryPilotToolkit.")
        return querypilot
    return querypilot.toolkit(**toolkit_options)


def tool_function(
    toolkit: QueryPilotToolkit, tool: QueryPilotTool, *, as_json: bool = False
) -> Callable[..., Any]:
    """A function with a real signature for one tool; ``as_json`` makes it return a JSON string."""
    name = tool.name

    def emit(payload: dict[str, Any]) -> Any:
        return to_json(payload) if as_json else payload

    if tool.key == "list_tables":

        def function() -> dict[str, Any]:
            return emit(toolkit.dispatch(name, {}))

    elif tool.key == "describe_table":

        def function(table_names: list[str]) -> dict[str, Any]:
            return emit(toolkit.dispatch(name, {"table_names": table_names}))

    elif tool.key == "search_schema":

        def function(query: str) -> dict[str, Any]:
            return emit(toolkit.dispatch(name, {"query": query}))

    elif tool.key in {"validate_sql", "run_sql"}:

        def function(sql: str) -> dict[str, Any]:
            return emit(toolkit.dispatch(name, {"sql": sql}))

    elif tool.key == "ask_database":

        def function(question: str) -> dict[str, Any]:
            return emit(toolkit.dispatch(name, {"question": question}))

    else:  # pragma: no cover - every key is handled above
        raise ValueError(f"No function signature for tool {tool.key}")

    function.__name__ = name
    function.__qualname__ = name
    function.__doc__ = docstring(tool)
    if as_json:
        function.__annotations__ = {**function.__annotations__, "return": str}
    return function


def docstring(tool: QueryPilotTool) -> str:
    """The tool description plus a Google-style ``Args:`` section."""
    properties = tool.parameters.get("properties", {})
    if not properties:
        return tool.description
    args = "\n".join(
        f"    {param}: {spec.get('description', '')}".rstrip() for param, spec in properties.items()
    )
    return f"{tool.description}\n\nArgs:\n{args}\n"


def args_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """A pydantic model for a tool's (flat, string or string-array) JSON-schema parameters."""
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for param, spec in schema.get("properties", {}).items():
        annotation: Any = list[str] if spec.get("type") == "array" else str
        default = ... if param in required else spec.get("default")
        fields[param] = (annotation, Field(default, description=spec.get("description")))
    model_name = "".join(part.title() for part in name.split("_")) + "Input"
    return create_model(model_name, **fields)
