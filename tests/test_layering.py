"""Boundary tests for the db / worker / agent layering.

These tests pin the architectural rule from ``AGENTS.md`` →
'Layering: db / worker / agent':

- agents never touch the database (no ``sqlmodel`` / ``sqlalchemy``
  imports, no session factory, no ``db.connection``);
- the worker layer goes through ``thematic_analysis_inc.db``'s public
  helpers and never imports a session factory or SQL DSL directly.

Model classes from ``db.models`` and pure adapters (constants,
domain-object converters) are fine in both layers — they're data, not
data *access*.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_SRC = Path(__file__).resolve().parent.parent / "src"


# Phrases that indicate the file is opening sessions / running SQL.
SQL_MACHINERY = (
    "from sqlmodel import",
    "import sqlmodel",
    "from sqlalchemy",
    "import sqlalchemy",
    "from thematic_analysis_inc.db.connection",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _offenders(text: str) -> list[str]:
    return [needle for needle in SQL_MACHINERY if needle in text]


def test_workers_does_not_open_sessions_directly():
    offenders = _offenders(
        _read(_SRC / "thematic_analysis_inc" / "workers.py")
    )
    assert not offenders, (
        "workers.py must go through the db.* helpers, but imports "
        f"{offenders}. See AGENTS.md → 'Layering: db / worker / agent'."
    )


@pytest.mark.parametrize(
    "agent_file",
    [
        "coder.py",
        "aggregator.py",
        "reviewer.py",
    ],
)
def test_agents_do_not_touch_the_database(agent_file: str):
    path = _SRC / "thematic_analysis" / "agents" / agent_file
    offenders = _offenders(_read(path))
    assert not offenders, (
        f"{agent_file} must not touch the database, but imports "
        f"{offenders}. See AGENTS.md → 'Layering: db / worker / agent'."
    )
