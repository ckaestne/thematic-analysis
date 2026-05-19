"""SQLite connection setup for the incremental pipeline.

Two layers coexist during the SQLModel migration:

- A raw ``sqlite3.Connection`` returned by :func:`connect` — drives the
  tables that haven't been migrated to SQLModel yet (``codes``,
  ``coding_queue``, …).
- A SQLAlchemy :class:`Engine` plus a :func:`session` factory — drives
  the tables that *have* been migrated. Currently: just
  ``research_context``.

Both layers point at the same on-disk database file, so a single
``connect(path)`` call sets both up.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlmodel import Session


def now() -> str:
    """ISO-8601 UTC timestamp with second precision (raw-SQL layer)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# SQLAlchemy engine (used by the SQLModel-migrated tables)
# ---------------------------------------------------------------------------


_engine: Engine | None = None
_engine_path: str | None = None


def get_engine() -> Engine:
    """Return the SQLAlchemy engine. ``connect()`` must have been called."""
    if _engine is None:
        raise RuntimeError(
            "SQLAlchemy engine not initialized — call connect(path) first"
        )
    return _engine


def session() -> Session:
    """Open a new SQLModel session against the active engine."""
    return Session(get_engine())


def _ensure_engine(path: str | Path) -> Engine:
    """Set up the SQLAlchemy engine for ``path``, replacing any previous
    engine pointing at a different path (matters in tests where each
    case uses its own ``tmp_path``)."""
    global _engine, _engine_path
    p = str(path)
    if _engine is not None and _engine_path != p:
        _engine.dispose()
        _engine = None
    if _engine is None:
        _engine = create_engine(
            f"sqlite:///{p}",
            connect_args={"check_same_thread": False},
        )
        _engine_path = p
    return _engine


def _create_sqlmodel_tables(engine: Engine) -> None:
    """Create the tables owned by SQLModel (idempotent).

    As more tables migrate from raw SQL to SQLModel, list them here.
    """
    # Local import to avoid circular dependencies at module load.
    from thematic_analysis_inc.db.models import ResearchContext

    ResearchContext.__table__.create(engine, checkfirst=True)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open an autocommit sqlite3 connection AND set up the SQLAlchemy
    engine, both pointing at ``path``. Both layers' schemas are applied
    so a fresh DB is usable immediately.

    We intentionally do NOT pass ``detect_types=sqlite3.PARSE_DECLTYPES``
    to the sqlite3 connection: the raw-SQL layer compares timestamps as
    ISO 8601 strings. SQLAlchemy handles datetime conversion natively on
    its side, independently.
    """
    from thematic_analysis_inc.db.schema import create_schema

    engine = _ensure_engine(path)
    _create_sqlmodel_tables(engine)

    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    create_schema(conn)
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    """Connect + apply schema + ensure codebook v1 exists."""
    from thematic_analysis_inc.db.codebook import (
        insert_codebook_version,
        latest_codebook_version,
    )

    conn = connect(path)
    if latest_codebook_version(conn) is None:
        insert_codebook_version(conn, parent=None, created_by="init")
    return conn
