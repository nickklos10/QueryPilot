from __future__ import annotations

import importlib
from types import ModuleType


def require(module: str, extra: str) -> ModuleType:
    """Import an optional dependency, naming the extra that installs it when missing."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(
            f"{module.split('.')[0]} is not installed. Install it with: "
            f'pip install "querypilot[{extra}]"'
        ) from exc
