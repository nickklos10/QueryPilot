"""Which agent tools run SQL, and where their input keeps it.

A query tool is mapped by its tool name (exact, or a glob such as
``mcp__warehouse__*``) and the input field that holds the SQL. Schema tools are
the ones whose results describe a dataset's columns; their results feed the
schema cache the guard validates against.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

FOUNDRY_SQL_TOOL = "mcp__palantir-mcp__run_sql_query_on_foundry_dataset"
FOUNDRY_SCHEMA_TOOL = "mcp__palantir-mcp__get_foundry_dataset_schema"


@dataclass(frozen=True)
class QueryToolSpec:
    name: str
    sql_field: str
    dialect: str = "spark"
    # Input field that caps returned rows, when the tool has one.
    row_limit_field: str | None = None

    def matches(self, tool_name: str) -> bool:
        return fnmatch.fnmatchcase(tool_name, self.name)


@dataclass(frozen=True)
class SchemaToolSpec:
    name: str
    # Parser for the tool's result; "foundry" is the only built-in format.
    format: str = "foundry"

    def matches(self, tool_name: str) -> bool:
        return fnmatch.fnmatchcase(tool_name, self.name)


DEFAULT_QUERY_TOOLS: tuple[QueryToolSpec, ...] = (
    QueryToolSpec(FOUNDRY_SQL_TOOL, "sqlQuery", "spark", row_limit_field="rowLimit"),
)
DEFAULT_SCHEMA_TOOLS: tuple[SchemaToolSpec, ...] = (SchemaToolSpec(FOUNDRY_SCHEMA_TOOL),)


@dataclass(frozen=True)
class ToolRegistry:
    query_tools: tuple[QueryToolSpec, ...] = DEFAULT_QUERY_TOOLS
    schema_tools: tuple[SchemaToolSpec, ...] = DEFAULT_SCHEMA_TOOLS

    def query_spec(self, tool_name: str) -> QueryToolSpec | None:
        return next((spec for spec in self.query_tools if spec.matches(tool_name)), None)

    def schema_spec(self, tool_name: str) -> SchemaToolSpec | None:
        return next((spec for spec in self.schema_tools if spec.matches(tool_name)), None)

    def tool_names(self) -> list[str]:
        return [spec.name for spec in (*self.query_tools, *self.schema_tools)]

    @classmethod
    def from_args(cls, tools: list[str] | None, *, include_defaults: bool = True) -> ToolRegistry:
        extra = tuple(parse_tool_spec(value) for value in tools or [])
        base = DEFAULT_QUERY_TOOLS if include_defaults else ()
        return cls(query_tools=(*extra, *base), schema_tools=DEFAULT_SCHEMA_TOOLS)


def parse_tool_spec(value: str) -> QueryToolSpec:
    """Parse ``NAME=SQL_FIELD[:DIALECT[:ROW_LIMIT_FIELD]]``.

    ``mcp__warehouse__run_query=query:snowflake`` maps the ``query`` input of
    that tool as Snowflake SQL.
    """
    name, sep, rest = value.partition("=")
    if not sep or not name.strip() or not rest.strip():
        raise ValueError(f"Expected TOOL_NAME=SQL_FIELD[:DIALECT[:ROW_LIMIT_FIELD]], got {value!r}")
    parts = rest.split(":")
    sql_field = parts[0].strip()
    dialect = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "spark"
    row_limit_field = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    return QueryToolSpec(name.strip(), sql_field, dialect, row_limit_field)
