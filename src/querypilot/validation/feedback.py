"""Turn validation and database errors into feedback a model can act on."""
from __future__ import annotations

import re

from querypilot.core.types import DatabaseSchema

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def clean_error(message: str) -> str:
    """Strip terminal escape codes and surrounding whitespace from an error message."""
    return _ANSI_ESCAPE.sub("", message).strip()


def repair_hints(errors: list[str], schema: DatabaseSchema) -> list[str]:
    """Suggest fixes (e.g. close column names) for validation errors."""
    return []
