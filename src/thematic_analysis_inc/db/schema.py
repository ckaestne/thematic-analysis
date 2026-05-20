"""Raw-SQL DDL for the Stage-2 theme_* tables.

Every Stage-1 table is owned by SQLModel (see :mod:`db.models`); it is
created via ``SQLModel.metadata.create_all`` in
:func:`db.connection._create_sqlmodel_tables`. Only the Stage-2 tables
(``theme_coders``, ``theme_coder_runs``, ``theme_aggregations``,
``theme_aggregation_inputs``) still live here as plain ``CREATE TABLE
IF NOT EXISTS`` statements.

FKs point at the Stage-1 ``codebook(version)`` table, which is created
by the SQLModel layer first. The research context active for any row
here is reachable via that codebook's ``research_context``.
"""

from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
-- ── Stage 2 (theme_* tables) ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS theme_coders (
    theme_coder_id  TEXT PRIMARY KEY,
    identity        TEXT NOT NULL,
    created_at      DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS theme_coder_runs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_coder_id            TEXT NOT NULL,
    codebook_version          INTEGER NOT NULL,
    status                    TEXT NOT NULL DEFAULT 'running',
    claimed_at                DATETIME NOT NULL,
    finished_at               DATETIME,
    result_json               TEXT,
    raw_response              TEXT,
    error                     TEXT,
    UNIQUE(theme_coder_id, codebook_version),
    FOREIGN KEY (theme_coder_id) REFERENCES theme_coders(theme_coder_id),
    FOREIGN KEY (codebook_version) REFERENCES codebook(version)
);
CREATE INDEX IF NOT EXISTS idx_theme_coder_runs_status  ON theme_coder_runs(status);
CREATE INDEX IF NOT EXISTS idx_theme_coder_runs_version ON theme_coder_runs(codebook_version);

CREATE TABLE IF NOT EXISTS theme_aggregations (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    codebook_version          INTEGER NOT NULL UNIQUE,
    status                    TEXT NOT NULL DEFAULT 'running',
    created_at                DATETIME NOT NULL,
    finished_at               DATETIME,
    result_json               TEXT,
    error                     TEXT,
    FOREIGN KEY (codebook_version) REFERENCES codebook(version)
);
CREATE INDEX IF NOT EXISTS idx_theme_agg_status ON theme_aggregations(status);

CREATE TABLE IF NOT EXISTS theme_aggregation_inputs (
    theme_aggregation_id  INTEGER NOT NULL,
    theme_coder_run_id    INTEGER NOT NULL,
    PRIMARY KEY(theme_aggregation_id, theme_coder_run_id),
    FOREIGN KEY (theme_aggregation_id) REFERENCES theme_aggregations(id),
    FOREIGN KEY (theme_coder_run_id) REFERENCES theme_coder_runs(id)
);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Apply the Stage-2 DDL to an open connection. Idempotent."""
    conn.executescript(SCHEMA_SQL)
