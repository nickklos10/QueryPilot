from __future__ import annotations

import inspect
from typing import Any

from querypilot import QueryPilot
from querypilot.adapters.toolkit import (
    READ_ONLY_ANNOTATIONS,
    QueryPilotTool,
    QueryPilotToolkit,
)
from querypilot.integrations.functions import tool_function


def create_mcp_server(
    querypilot: QueryPilot,
    *,
    fastmcp_cls: type | None = None,
    name: str = "QueryPilot",
    **toolkit_options: Any,
):
    """Build an MCP server exposing QueryPilot's read-only tools.

    Works with mcp 2.x (``MCPServer``) and 1.x (``FastMCP``). Every tool is annotated
    read-only and returns errors as ``{"ok": False, "error", "hints"}`` instead of raising.
    """
    server_cls = fastmcp_cls or _load_server_cls()
    toolkit = querypilot.toolkit(**toolkit_options)
    kwargs: dict[str, Any] = {"instructions": toolkit.instructions()}
    if _accepts(server_cls, "stateless_http"):
        # mcp 1.x configures HTTP behaviour on the constructor; 2.x moved it to run().
        kwargs.update(stateless_http=True, json_response=True)
    server = server_cls(name, **kwargs)

    for tool in toolkit.tools:
        options = {
            "name": tool.name,
            "title": tool.title,
            "description": tool.description,
            "annotations": _annotations(tool),
        }
        # Older mcp 1.x releases predate some of these keywords.
        options = {key: value for key, value in options.items() if _accepts(server.tool, key)}
        server.tool(**options)(_compact_tool_function(toolkit, tool))
    return server


def _compact_tool_function(toolkit: QueryPilotToolkit, tool: QueryPilotTool) -> Any:
    # Return compact JSON text: MCP pretty-prints dict results, which can double the
    # tokens a result costs against client output caps.
    function = tool_function(toolkit, tool, as_json=True)
    signature = inspect.signature(function)
    function.__signature__ = signature.replace(return_annotation=str)
    function.__annotations__ = {**function.__annotations__, "return": str}
    return function


def run_mcp_server(server: Any, transport: str = "stdio", *, host: str | None = None, port: int | None = None) -> None:
    """Run a server from :func:`create_mcp_server`, stateless with JSON responses over HTTP."""
    if transport == "stdio":
        server.run(transport="stdio")
        return
    options: dict[str, Any] = {}
    http_runner = getattr(server, "run_streamable_http_async", None)
    if transport == "streamable-http" and http_runner is not None and _accepts(http_runner, "stateless_http"):
        options.update(stateless_http=True, json_response=True)
    if host is not None:
        options["host"] = host
    if port is not None:
        options["port"] = port
    if options and not _accepts(server.run, "host"):
        # mcp 1.x reads these from constructor settings instead of run().
        for key, value in options.items():
            setattr(server.settings, key, value)
        options = {}
    server.run(transport=transport, **options)


def _annotations(tool: QueryPilotTool) -> Any:
    try:
        from mcp.types import ToolAnnotations
    except ImportError:
        return {"title": tool.title, **READ_ONLY_ANNOTATIONS}
    return ToolAnnotations(title=tool.title, **READ_ONLY_ANNOTATIONS)


def _accepts(callable_: Any, parameter: str) -> bool:
    try:
        parameters = inspect.signature(callable_).parameters
    except (TypeError, ValueError):
        return False
    return parameter in parameters or any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )


def _load_server_cls() -> type:
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        pass
    else:
        return MCPServer
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError('Install the MCP extra to run the server: pip install "querypilot[mcp]"') from exc
    return FastMCP
