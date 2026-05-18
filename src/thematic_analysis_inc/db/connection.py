"""SQLite connection setup for the incremental pipeline."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    """ISO-8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open an autocommit connection with WAL + foreign keys + row factory.

    `create_schema` is applied on every connect so a fresh DB is usable
    immediately.
    """
    from thematic_analysis_inc.db.schema import create_schema

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
