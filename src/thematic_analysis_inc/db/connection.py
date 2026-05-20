"""SQLite connection setup for the incremental pipeline.

Two layers coexist:

- A SQLAlchemy :class:`Engine` plus a :func:`session` factory — drives
  every Stage-1 table (research_context, document, segment, code,
  codebook, coder, quote, coding_queue, codebook_code,
  codes_supporting_quotes, codes_derived).
- A raw ``sqlite3.Connection`` returned by :func:`connect` — still used
  by Stage-2 (``theme_*``) helpers in ``db/theme.py``.

Both layers point at the same on-disk database file.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlmodel import Session, SQLModel


def now() -> str:
    """ISO-8601 UTC timestamp with second precision (Stage-2 raw-SQL layer)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# SQLAlchemy engine (used by every Stage-1 helper)
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
    """Create every Stage-1 SQLModel-owned table (idempotent)."""
    from thematic_analysis_inc.db.models import (
        Code,
        Codebook,
        CodebookCode,
        Coder,
        CodesDerived,
        CodesSupportingQuotes,
        CodingQueueEntry,
        Document,
        Quote,
        ResearchContext,
        Segment,
    )

    SQLModel.metadata.create_all(
        engine,
        tables=[
            ResearchContext.__table__,
            Codebook.__table__,
            Coder.__table__,
            Document.__table__,
            Segment.__table__,
            Code.__table__,
            Quote.__table__,
            CodingQueueEntry.__table__,
            CodebookCode.__table__,
            CodesSupportingQuotes.__table__,
            CodesDerived.__table__,
        ],
    )

    # Seed system coders 0 (aggregator) and -1 (reviewer) once.
    with Session(engine) as s:
        if s.get(Coder, 0) is None:
            s.add(Coder(coder_id=0, identity="aggregator system coder"))
        if s.get(Coder, -1) is None:
            s.add(Coder(coder_id=-1, identity="reviewer system coder"))
        s.commit()


def _migrate_sqlmodel_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('segment')")
        }
        if "title" not in cols:
            conn.exec_driver_sql("ALTER TABLE segment ADD COLUMN title TEXT")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open an autocommit sqlite3 connection AND set up the SQLAlchemy
    engine, both pointing at ``path``. Both layers' schemas are applied
    so a fresh DB is usable immediately.

    The sqlite3 connection is only needed by Stage-2 helpers in
    ``db/theme.py``. Stage-1 callers should ignore the return value.
    """
    from thematic_analysis_inc.db.schema import create_schema

    engine = _ensure_engine(path)
    _create_sqlmodel_tables(engine)
    _migrate_sqlmodel_tables(engine)

    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    create_schema(conn)
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    """Connect + apply schema + ensure an initial empty research-context
    revision and the codebook revision pinned to it exist.

    ``set_research_context`` creates both the research-context row and a
    matching codebook revision, so seeding the empty context is enough
    to give every downstream FK something real to point at.
    """
    from thematic_analysis_inc.db.research_context import (
        latest_research_context_version,
        set_research_context,
    )
    from thematic_analysis.research_context import (
        ResearchContext as DomainResearchContext,
    )

    conn = connect(path)
    if latest_research_context_version() is None:
        set_research_context(DomainResearchContext(description=""))
    return conn
