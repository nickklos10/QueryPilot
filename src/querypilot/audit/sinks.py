from __future__ import annotations

from pathlib import Path
from typing import Protocol

from querypilot.audit.types import QueryAuditRecord


class AuditSink(Protocol):
    def write(self, record: QueryAuditRecord) -> None:
        ...

    def recent(self, limit: int = 100) -> list[QueryAuditRecord]:
        ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[QueryAuditRecord] = []

    def write(self, record: QueryAuditRecord) -> None:
        self.records.append(record)

    def recent(self, limit: int = 100) -> list[QueryAuditRecord]:
        return list(reversed(self.records[-limit:]))


class JSONLAuditSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._records: list[QueryAuditRecord] = []

    def write(self, record: QueryAuditRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
        self._records.append(record)

    def recent(self, limit: int = 100) -> list[QueryAuditRecord]:
        if self._records:
            return list(reversed(self._records[-limit:]))
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        records = [QueryAuditRecord.model_validate_json(line) for line in lines]
        return list(reversed(records))
