"""SQLite schema for the incremental Stage 1 pipeline.

Idempotent via `CREATE TABLE IF NOT EXISTS`. No migration framework — if
the schema needs to change before there are real users, drop the DB.
"""

from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
-- ── Research context (singleton row) ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS research_context (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    context_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- ── Stage 1 ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS codebook_versions (
    version         INTEGER PRIMARY KEY,
    parent_version  INTEGER,
    snapshot_json   TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (parent_version) REFERENCES codebook_versions(version)
);

CREATE TABLE IF NOT EXISTS coders (
    coder_id    TEXT PRIMARY KEY,
    identity    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    segment_id  TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    batch       INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_segments_status ON segments(status);

CREATE TABLE IF NOT EXISTS coder_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id        TEXT NOT NULL,
    coder_id          TEXT NOT NULL,
    codebook_version  INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'running',
    claimed_at        TEXT,
    finished_at       TEXT,
    raw_response      TEXT,
    error             TEXT,
    UNIQUE(segment_id, coder_id),
    FOREIGN KEY (segment_id) REFERENCES segments(segment_id),
    FOREIGN KEY (coder_id) REFERENCES coders(coder_id),
    FOREIGN KEY (codebook_version) REFERENCES codebook_versions(version)
);
CREATE INDEX IF NOT EXISTS idx_coder_runs_status ON coder_runs(status);
CREATE INDEX IF NOT EXISTS idx_coder_runs_segment ON coder_runs(segment_id);
CREATE INDEX IF NOT EXISTS idx_coder_runs_coder ON coder_runs(coder_id);

CREATE TABLE IF NOT EXISTS coder_codes (
    coder_run_id  INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    code          TEXT NOT NULL,
    rationale     TEXT,
    is_new        INTEGER,
    PRIMARY KEY (coder_run_id, position),
    FOREIGN KEY (coder_run_id) REFERENCES coder_runs(id)
);

CREATE TABLE IF NOT EXISTS aggregations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id   TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    finished_at  TEXT,
    error        TEXT,
    FOREIGN KEY (segment_id) REFERENCES segments(segment_id)
);
CREATE INDEX IF NOT EXISTS idx_aggregations_status ON aggregations(status);

CREATE TABLE IF NOT EXISTS aggregated_codes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregation_id      INTEGER NOT NULL,
    code                TEXT NOT NULL,
    quotes_json         TEXT NOT NULL,
    source_coders_json  TEXT NOT NULL,
    FOREIGN KEY (aggregation_id) REFERENCES aggregations(id)
);
CREATE INDEX IF NOT EXISTS idx_aggcodes_agg ON aggregated_codes(aggregation_id);

CREATE TABLE IF NOT EXISTS review_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregated_code_id  INTEGER NOT NULL UNIQUE,
    decision            TEXT NOT NULL,
    target_code         TEXT,
    rationale           TEXT,
    applied             INTEGER NOT NULL DEFAULT 0,
    resulting_version   INTEGER,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (aggregated_code_id) REFERENCES aggregated_codes(id),
    FOREIGN KEY (resulting_version) REFERENCES codebook_versions(version)
);

-- ── Stage 2 ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS theme_coders (
    theme_coder_id  TEXT PRIMARY KEY,
    identity        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- One run per (theme_coder, codebook_version). Created lazily when the coder
-- starts work. A coder produces exactly one ThemeResult per codebook version.
CREATE TABLE IF NOT EXISTS theme_coder_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_coder_id   TEXT NOT NULL,
    codebook_version INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running',
    claimed_at       TEXT NOT NULL,
    finished_at      TEXT,
    result_json      TEXT,
    raw_response     TEXT,
    error            TEXT,
    UNIQUE(theme_coder_id, codebook_version),
    FOREIGN KEY (theme_coder_id) REFERENCES theme_coders(theme_coder_id),
    FOREIGN KEY (codebook_version) REFERENCES codebook_versions(version)
);
CREATE INDEX IF NOT EXISTS idx_theme_coder_runs_status  ON theme_coder_runs(status);
CREATE INDEX IF NOT EXISTS idx_theme_coder_runs_version ON theme_coder_runs(codebook_version);

-- One aggregation per codebook_version. Created when all theme coders are done.
CREATE TABLE IF NOT EXISTS theme_aggregations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    codebook_version INTEGER NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'running',
    created_at       TEXT NOT NULL,
    finished_at      TEXT,
    result_json      TEXT,
    error            TEXT,
    FOREIGN KEY (codebook_version) REFERENCES codebook_versions(version)
);
CREATE INDEX IF NOT EXISTS idx_theme_agg_status ON theme_aggregations(status);

-- Which theme_coder_runs contributed to each aggregation.
CREATE TABLE IF NOT EXISTS theme_aggregation_inputs (
    theme_aggregation_id  INTEGER NOT NULL,
    theme_coder_run_id    INTEGER NOT NULL,
    PRIMARY KEY(theme_aggregation_id, theme_coder_run_id),
    FOREIGN KEY (theme_aggregation_id) REFERENCES theme_aggregations(id),
    FOREIGN KEY (theme_coder_run_id) REFERENCES theme_coder_runs(id)
);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Apply the schema to an open connection. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()
