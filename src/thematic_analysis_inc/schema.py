"""SQLite schema for the incremental Stage 1 pipeline.

Idempotent via `CREATE TABLE IF NOT EXISTS`. No migration framework — if
the schema needs to change before there are real users, drop the DB.
"""

from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
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
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Apply the schema to an open connection. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()
