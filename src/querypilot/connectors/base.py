from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from querypilot.core.types import DatabaseSchema


class BaseConnector(ABC):
    def __init__(self, database_url: str, timeout_seconds: int = 10) -> None:
        self.database_url = database_url
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    def get_schema(self) -> DatabaseSchema:
        raise NotImplementedError

    @abstractmethod
    def execute_readonly(self, sql: str) -> tuple[list[dict], int]:
        raise NotImplementedError

    @staticmethod
    def rows_to_dicts(keys: Sequence[str], rows: Sequence[Sequence[object]]) -> list[dict]:
        return [dict(zip(keys, row, strict=False)) for row in rows]
