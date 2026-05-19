"""DDL for the incremental Stage 1 + Stage 2 pipeline.

Idempotent via `CREATE TABLE IF NOT EXISTS`. No migrations: if the
schema changes before there are real users, drop the DB.

Datetime columns use the SQLite declared type ``DATETIME`` for clarity
(documenting them as ISO 8601 timestamps). We deliberately do *not*
enable ``sqlite3.PARSE_DECLTYPES`` on the connection: timestamps stay
plain ISO strings on the Python side so that the many comparison sites
(``finished_at IS NOT NULL``, equality checks in tests, etc.) keep
working unchanged. The declared type is purely documentation.
"""

from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
-- ── Research context (versioned history; latest row = current) ───────────────

CREATE TABLE IF NOT EXISTS research_context (
    research_context_version  INTEGER PRIMARY KEY AUTOINCREMENT,
    description               TEXT NOT NULL DEFAULT '',
    coder_prompt              TEXT,
    coding_critic_prompt      TEXT,
    reviewer_prompt           TEXT,
    theme_coder_prompt        TEXT,
    theme_aggregator_prompt   TEXT,
    created_at                DATETIME NOT NULL
);

-- ── Stage 1 ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS codebook_versions (
    version         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_version  INTEGER,
    created_by      TEXT NOT NULL,
    created_at      DATETIME NOT NULL,
    FOREIGN KEY (parent_version) REFERENCES codebook_versions(version)
);

CREATE TABLE IF NOT EXISTS coders (
    coder_id    INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    identity    TEXT NOT NULL,
    created_at  DATETIME NOT NULL
);

INSERT OR IGNORE INTO coders (coder_id, name, identity, created_at)
VALUES (0, 'aggregator', 'aggregator system coder', '1970-01-01T00:00:00+00:00');
INSERT OR IGNORE INTO coders (coder_id, name, identity, created_at)
VALUES (-1, 'reviewer', 'reviewer system coder', '1970-01-01T00:00:00+00:00');

CREATE TABLE IF NOT EXISTS documents (
    document_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT NOT NULL,
    content      BLOB NOT NULL,
    created_at   DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_filename ON documents(filename);

CREATE TABLE IF NOT EXISTS segments (
    segment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER REFERENCES documents(document_id),
    content      TEXT NOT NULL,
    line_from    INTEGER,
    line_to      INTEGER,
    position     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_segments_document ON segments(document_id);

CREATE TABLE IF NOT EXISTS codes (
    code_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id                INTEGER REFERENCES segments(segment_id),
    coder_id                  INTEGER NOT NULL REFERENCES coders(coder_id),
    codebook_version          INTEGER REFERENCES codebook_versions(version),
    research_context_version  INTEGER REFERENCES research_context(research_context_version),
    code                      TEXT NOT NULL,
    description               TEXT NOT NULL DEFAULT '',
    rationale                 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_codes_segment ON codes(segment_id);
CREATE INDEX IF NOT EXISTS idx_codes_coder ON codes(coder_id);
CREATE INDEX IF NOT EXISTS idx_codes_codebook_version ON codes(codebook_version);

CREATE TABLE IF NOT EXISTS quotes (
    quote_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id  INTEGER REFERENCES segments(segment_id),
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_segment ON quotes(segment_id);

CREATE TABLE IF NOT EXISTS codes_supporting_quotes (
    code_id   INTEGER NOT NULL REFERENCES codes(code_id),
    quote_id  INTEGER NOT NULL REFERENCES quotes(quote_id),
    PRIMARY KEY (code_id, quote_id)
);

CREATE TABLE IF NOT EXISTS codes_derived (
    new_code_id      INTEGER NOT NULL REFERENCES codes(code_id),
    source_code_id   INTEGER NOT NULL REFERENCES codes(code_id),
    derivation_type  TEXT NOT NULL CHECK (derivation_type IN ('A','R')),
    decision         TEXT CHECK (decision IS NULL OR decision IN ('A','M','U')),
    rationale        TEXT,
    PRIMARY KEY (new_code_id, source_code_id)
);
CREATE INDEX IF NOT EXISTS idx_codes_derived_src
    ON codes_derived(source_code_id, derivation_type);

CREATE TABLE IF NOT EXISTS codebook (
    version  INTEGER NOT NULL REFERENCES codebook_versions(version),
    code_id  INTEGER NOT NULL REFERENCES codes(code_id),
    PRIMARY KEY (version, code_id)
);
CREATE INDEX IF NOT EXISTS idx_codebook_version ON codebook(version);

CREATE TABLE IF NOT EXISTS coding_queue (
    segment_id                INTEGER NOT NULL REFERENCES segments(segment_id),
    coder_id                  INTEGER NOT NULL REFERENCES coders(coder_id),
    codebook_version          INTEGER NOT NULL REFERENCES codebook_versions(version),
    research_context_version  INTEGER REFERENCES research_context(research_context_version),
    claimed_at                DATETIME,
    finished_at               DATETIME,
    error                     TEXT,
    PRIMARY KEY (segment_id, coder_id)
);
CREATE INDEX IF NOT EXISTS idx_coding_queue_coder ON coding_queue(coder_id);

-- ── Stage 2 (unchanged) ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS theme_coders (
    theme_coder_id  TEXT PRIMARY KEY,
    identity        TEXT NOT NULL,
    created_at      DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS theme_coder_runs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_coder_id            TEXT NOT NULL,
    codebook_version          INTEGER NOT NULL,
    research_context_version  INTEGER REFERENCES research_context(research_context_version),
    status                    TEXT NOT NULL DEFAULT 'running',
    claimed_at                DATETIME NOT NULL,
    finished_at               DATETIME,
    result_json               TEXT,
    raw_response              TEXT,
    error                     TEXT,
    UNIQUE(theme_coder_id, codebook_version),
    FOREIGN KEY (theme_coder_id) REFERENCES theme_coders(theme_coder_id),
    FOREIGN KEY (codebook_version) REFERENCES codebook_versions(version)
);
CREATE INDEX IF NOT EXISTS idx_theme_coder_runs_status  ON theme_coder_runs(status);
CREATE INDEX IF NOT EXISTS idx_theme_coder_runs_version ON theme_coder_runs(codebook_version);

CREATE TABLE IF NOT EXISTS theme_aggregations (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    codebook_version          INTEGER NOT NULL UNIQUE,
    research_context_version  INTEGER REFERENCES research_context(research_context_version),
    status                    TEXT NOT NULL DEFAULT 'running',
    created_at                DATETIME NOT NULL,
    finished_at               DATETIME,
    result_json               TEXT,
    error                     TEXT,
    FOREIGN KEY (codebook_version) REFERENCES codebook_versions(version)
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
    """Apply the schema to an open connection. Idempotent."""
    conn.executescript(SCHEMA_SQL)
