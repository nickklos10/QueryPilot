"""Apply configured descriptions to an introspected schema.

``config.descriptions`` is keyed by ``"table"`` or ``"table.column"`` (matched
case-insensitively) and overrides database comments. Sample values are not
added here: they are fetched lazily per table for the prompt (see
``querypilot.schema.sampling``), so the cached schema never holds data values.
"""
from __future__ import annotations

from querypilot.connectors.base import BaseConnector
from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import DatabaseSchema


def enrich_schema(
    schema: DatabaseSchema,
    connector: BaseConnector,
    config: QueryPilotConfig,
) -> DatabaseSchema:
    descriptions = {key.lower(): value for key, value in config.descriptions.items()}
    if not descriptions:
        return schema
    for table in schema.tables:
        table_key = table.name.lower()
        if table_key in descriptions:
            table.comment = descriptions[table_key]
        for column in table.columns:
            key = f"{table_key}.{column.name.lower()}"
            if key in descriptions:
                column.comment = descriptions[key]
    return schema
