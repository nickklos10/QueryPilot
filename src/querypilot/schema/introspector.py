"""Read tables, views, keys and comments from any SQLAlchemy engine."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.reflection import Inspector, ObjectKind
from sqlalchemy.exc import SQLAlchemyError

from querypilot.core.types import ColumnSchema, DatabaseSchema, ForeignKey, TableSchema

if TYPE_CHECKING:
    from querypilot.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_INTERNAL_PREFIXES = {"sqlite": ("sqlite_",)}


def introspect(connector: BaseConnector) -> DatabaseSchema:
    return connector.get_schema()


def introspect_engine(
    engine: Engine,
    dialect: str,
    schemas: list[str] | None = None,
) -> DatabaseSchema:
    """Introspect tables and views with their columns, keys and comments.

    With ``schemas`` given, each table records its ``schema_name``; otherwise the
    connection's default schema is read. A table that fails to reflect is skipped.
    """
    if engine.dialect.name == "duckdb":
        tables = _duckdb_tables(engine, schemas)
    else:
        inspector = inspect(engine)
        tables = []
        for schema in schemas or [None]:
            tables.extend(_inspector_tables(inspector, dialect, schema))
    return DatabaseSchema(dialect=dialect, tables=tables)


def _inspector_tables(inspector: Inspector, dialect: str, schema: str | None) -> list[TableSchema]:
    prefixes = _INTERNAL_PREFIXES.get(dialect, ())
    table_names = [
        name for name in _list_names(inspector.get_table_names, schema) if not name.startswith(prefixes)
    ]
    view_names = [
        name for name in _list_names(inspector.get_view_names, schema) if not name.startswith(prefixes)
    ]
    names = table_names + view_names

    columns = _reflect(inspector, "columns", schema, names)
    primary_keys = _reflect(inspector, "pk_constraint", schema, table_names)
    foreign_keys = _reflect(inspector, "foreign_keys", schema, table_names)
    comments = _reflect(inspector, "table_comment", schema, names)

    tables: list[TableSchema] = []
    for name in names:
        if name not in columns:
            continue
        try:
            tables.append(
                _build_table(
                    name,
                    schema,
                    columns[name],
                    primary_keys.get(name) or {},
                    foreign_keys.get(name) or [],
                    (comments.get(name) or {}).get("text"),
                    is_view=name in view_names,
                )
            )
        except Exception:
            logger.warning("Skipping table %s: could not read its definition", name, exc_info=True)
    return tables


def _list_names(method, schema: str | None) -> list[str]:
    try:
        return list(method(schema=schema))
    except NotImplementedError:
        return []


def _reflect(inspector: Inspector, what: str, schema: str | None, names: list[str]) -> dict[str, Any]:
    """Reflect ``what`` for many tables at once, falling back to one table at a time."""
    if not names:
        return {}
    try:
        multi = getattr(inspector, f"get_multi_{what}")(
            schema=schema, filter_names=names, kind=ObjectKind.ANY
        )
        return {key[1]: value for key, value in multi.items()}
    except Exception:
        logger.debug("Bulk %s reflection failed; reflecting tables one by one", what, exc_info=True)

    single = getattr(inspector, f"get_{what}")
    reflected: dict[str, Any] = {}
    for name in names:
        try:
            reflected[name] = single(name, schema=schema)
        except NotImplementedError:
            return reflected
        except Exception:
            logger.warning("Could not reflect %s for table %s", what, name, exc_info=True)
    return reflected


def _build_table(
    name: str,
    schema: str | None,
    columns: list[dict],
    primary_key: dict,
    foreign_keys: list[dict],
    comment: str | None,
    *,
    is_view: bool,
) -> TableSchema:
    pk_columns = set(primary_key.get("constrained_columns") or [])
    return TableSchema(
        name=name,
        schema_name=schema,
        comment=comment,
        is_view=is_view,
        columns=[
            ColumnSchema(
                name=column["name"],
                type=_type_name(column["type"]),
                nullable=bool(column.get("nullable", True)),
                primary_key=column["name"] in pk_columns,
                comment=column.get("comment"),
            )
            for column in columns
        ],
        foreign_keys=[
            ForeignKey(
                columns=list(fk["constrained_columns"]),
                referred_table=_qualified(fk.get("referred_schema"), fk["referred_table"], schema),
                referred_columns=list(fk["referred_columns"]),
            )
            for fk in foreign_keys
            if fk.get("referred_table")
        ],
    )


def _type_name(column_type: Any) -> str:
    try:
        return str(column_type)
    except SQLAlchemyError:
        return type(column_type).__name__


def _qualified(referred_schema: str | None, table: str, schema: str | None) -> str:
    if referred_schema and referred_schema != schema:
        return f"{referred_schema}.{table}"
    return table


# duckdb-engine reflects through Postgres catalog emulation that DuckDB does not fully
# provide, so DuckDB is read from its own catalog functions instead.
_DUCKDB_RELATIONS = """
SELECT schema_name, table_name AS name, comment, FALSE AS is_view
FROM duckdb_tables()
WHERE database_name = current_database() AND NOT internal AND NOT temporary
UNION ALL
SELECT schema_name, view_name AS name, comment, TRUE AS is_view
FROM duckdb_views()
WHERE database_name = current_database() AND NOT internal AND NOT temporary
"""

_DUCKDB_COLUMNS = """
SELECT schema_name, table_name, column_name, data_type, is_nullable, comment
FROM duckdb_columns()
WHERE database_name = current_database() AND NOT internal
ORDER BY schema_name, table_name, column_index
"""

_DUCKDB_CONSTRAINTS = """
SELECT schema_name, table_name, constraint_type, constraint_column_names,
       referenced_table, referenced_column_names
FROM duckdb_constraints()
WHERE database_name = current_database()
  AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
"""


def _duckdb_tables(engine: Engine, schemas: list[str] | None) -> list[TableSchema]:
    with engine.connect() as conn:
        default_schema = conn.execute(text("SELECT current_schema()")).scalar()
        relations = conn.execute(text(_DUCKDB_RELATIONS)).mappings().all()
        column_rows = conn.execute(text(_DUCKDB_COLUMNS)).mappings().all()
        constraint_rows = conn.execute(text(_DUCKDB_CONSTRAINTS)).mappings().all()

    wanted = set(schemas or [default_schema])
    columns: dict[tuple[str, str], list[dict]] = {}
    for row in column_rows:
        columns.setdefault((row["schema_name"], row["table_name"]), []).append(
            {
                "name": row["column_name"],
                "type": row["data_type"],
                "nullable": row["is_nullable"],
                "comment": row["comment"],
            }
        )
    primary_keys: dict[tuple[str, str], dict] = {}
    foreign_keys: dict[tuple[str, str], list[dict]] = {}
    for row in constraint_rows:
        key = (row["schema_name"], row["table_name"])
        if row["constraint_type"] == "PRIMARY KEY":
            primary_keys[key] = {"constrained_columns": list(row["constraint_column_names"])}
        else:
            foreign_keys.setdefault(key, []).append(
                {
                    "constrained_columns": list(row["constraint_column_names"]),
                    "referred_table": row["referenced_table"],
                    "referred_columns": list(row["referenced_column_names"]),
                }
            )

    tables: list[TableSchema] = []
    for relation in relations:
        schema_name = relation["schema_name"]
        key = (schema_name, relation["name"])
        if schema_name not in wanted or key not in columns:
            continue
        try:
            tables.append(
                _build_table(
                    relation["name"],
                    schema_name if schemas else None,
                    columns[key],
                    primary_keys.get(key, {}),
                    foreign_keys.get(key, []),
                    relation["comment"],
                    is_view=bool(relation["is_view"]),
                )
            )
        except Exception:
            logger.warning("Skipping table %s: could not read its definition", key[1], exc_info=True)
    return tables
