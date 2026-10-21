"""Add descriptions and sample values to an introspected schema."""
from __future__ import annotations

from querypilot.connectors.base import BaseConnector
from querypilot.core.config import QueryPilotConfig
from querypilot.core.types import DatabaseSchema


def enrich_schema(
    schema: DatabaseSchema,
    connector: BaseConnector,
    config: QueryPilotConfig,
) -> DatabaseSchema:
    for table in schema.tables:
        if table.name in config.descriptions:
            table.comment = config.descriptions[table.name]
        for column in table.columns:
            key = f"{table.name}.{column.name}"
            if key in config.descriptions:
                column.comment = config.descriptions[key]
    return schema
