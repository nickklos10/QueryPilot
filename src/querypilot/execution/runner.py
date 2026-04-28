from __future__ import annotations

import time

from querypilot.connectors.base import BaseConnector
from querypilot.core.types import QueryResult


def execute(connector: BaseConnector, sql: str) -> QueryResult:
    start = time.perf_counter()
    rows, row_count = connector.execute_readonly(sql)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return QueryResult(sql=sql, rows=rows, row_count=row_count, execution_time_ms=elapsed_ms)
