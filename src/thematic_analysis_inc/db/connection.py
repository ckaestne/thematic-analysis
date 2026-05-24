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

from sqlalchemy import Engine, create_engine, event
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

        @event.listens_for(_engine, "connect")
        def _enable_sqlite_fks(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        _engine_path = p
    return _engine


def _create_sqlmodel_tables(engine: Engine) -> None:
    """Create every SQLModel-owned table (idempotent)."""
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
        Theme,
        ThemeCode,
        ThemeCodingJob,
        ThemeSupportingQuote,
        ThemesDerived,
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
            # Stage 2
            ThemeCodingJob.__table__,
            Theme.__table__,
            ThemeCode.__table__,
            ThemeSupportingQuote.__table__,
            ThemesDerived.__table__,
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
        seg_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('segment')")
        }
        if "title" not in seg_cols:
            conn.exec_driver_sql("ALTER TABLE segment ADD COLUMN title TEXT")

        code_cols = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info('code')")
        }
        if "embedding" not in code_cols:
            conn.exec_driver_sql("ALTER TABLE code ADD COLUMN embedding BLOB")

        rc_cols = {
            row[1]
            for row in conn.exec_driver_sql(
                "PRAGMA table_info('research_context')"
            )
        }
        if "theme_coder_prompt" not in rc_cols:
            conn.exec_driver_sql(
                "ALTER TABLE research_context "
                "ADD COLUMN theme_coder_prompt TEXT"
            )


def connect(path: str | Path) -> sqlite3.Connection:
    """Open an autocommit sqlite3 connection AND set up the SQLAlchemy
    engine, both pointing at ``path``. The Stage-1 schema is applied so a
    fresh DB is usable immediately.

    Stage-1 callers go through the SQLModel session and don't need the
    sqlite3 connection; it is returned for the few raw-SQL call sites
    that still want it.
    """
    engine = _ensure_engine(path)
    _create_sqlmodel_tables(engine)
    _migrate_sqlmodel_tables(engine)

    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    """Connect + apply schema + ensure an initial empty research-context
    revision and the codebook revision pinned to it exist.

    ``add_research_context_and_codebook_revision`` creates both rows in
    one transaction, so seeding the empty context is enough to give
    every downstream FK something real to point at.
    """
    from thematic_analysis_inc.db.research_context import (
        add_research_context_and_codebook_revision,
        latest_research_context_version,
    )
    from thematic_analysis.research_context import (
        ResearchContext as DomainResearchContext,
    )

    conn = connect(path)
    if latest_research_context_version() is None:
        add_research_context_and_codebook_revision(
            DomainResearchContext(description="")
        )
    return conn
