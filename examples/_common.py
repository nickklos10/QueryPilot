"""Shared helpers for the QueryPilot examples.

Nothing here is required to use QueryPilot in your own project -- it only
resolves the path to the bundled demo SQLite fixture so every example can run
from a clean checkout without configuration.
"""

from __future__ import annotations

from pathlib import Path

# examples/ -> repo root -> tests/fixtures/demo.db
_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "demo.db"


def demo_database_url() -> str:
    """Return a ``sqlite:///`` URL for the bundled demo fixture.

    If the fixture is missing (fresh clone), seed it with::

        python tests/fixtures/seed_demo.py
    """
    if not _FIXTURE.exists():
        raise SystemExit(
            f"Demo fixture not found at {_FIXTURE}.\n"
            "Seed it first:  python tests/fixtures/seed_demo.py"
        )
    # _FIXTURE is absolute (starts with '/'), so this yields the sqlite:////abs form.
    return f"sqlite:///{_FIXTURE}"
