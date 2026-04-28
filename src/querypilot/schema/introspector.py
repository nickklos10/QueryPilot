from __future__ import annotations

from querypilot.connectors.base import BaseConnector
from querypilot.core.types import DatabaseSchema


def introspect(connector: BaseConnector) -> DatabaseSchema:
    return connector.get_schema()
