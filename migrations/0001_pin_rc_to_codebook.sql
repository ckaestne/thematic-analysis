-- Migrate an existing thematic-analysis SQLite DB to the new schema where
-- `codebook` pins a `research_context_version` and downstream tables drop
-- their redundant `research_context_used_id` / `research_context_version`
-- columns.
--
-- Existing codebook rows are pinned to the latest research_context_version
-- that exists at migration time (best available proxy for "the RC in
-- effect when this revision was created"). If your DB has multiple RC
-- revisions and you care about per-codebook accuracy, edit the UPDATE
-- below before running.
--
-- Run with:  sqlite3 your.db < 0001_pin_rc_to_codebook.sql
--
-- Requires SQLite >= 3.35 (for ALTER TABLE ... DROP COLUMN).

BEGIN;

PRAGMA foreign_keys = OFF;

-- 1. codebook: add research_context_version, backfill, then enforce NOT NULL
--    via table rebuild.
ALTER TABLE codebook
    ADD COLUMN research_context_version INTEGER
        REFERENCES research_context(research_context_version);

UPDATE codebook
SET research_context_version = (
    SELECT MAX(research_context_version) FROM research_context
);

CREATE TABLE codebook__new (
    version                  INTEGER PRIMARY KEY,
    parent_version           INTEGER REFERENCES codebook(version),
    research_context_version INTEGER NOT NULL
        REFERENCES research_context(research_context_version),
    created_at               DATETIME NOT NULL
);
INSERT INTO codebook__new (version, parent_version, research_context_version, created_at)
SELECT version, parent_version, research_context_version, created_at FROM codebook;
DROP TABLE codebook;
ALTER TABLE codebook__new RENAME TO codebook;
CREATE INDEX ix_codebook_research_context_version
    ON codebook(research_context_version);

-- 2. code: drop research_context_used_id.
ALTER TABLE code DROP COLUMN research_context_used_id;

-- 3. coding_queue: drop research_context_used_id from columns AND from the
--    composite PK. Rebuild the table to change the primary key.
CREATE TABLE coding_queue__new (
    segment_id       INTEGER NOT NULL REFERENCES segment(segment_id),
    coder_id         INTEGER NOT NULL REFERENCES coder(coder_id),
    codebook_used_id INTEGER NOT NULL REFERENCES codebook(version),
    enqueued_at      DATETIME NOT NULL,
    claimed_at       DATETIME,
    finished_at      DATETIME,
    error            TEXT,
    PRIMARY KEY (segment_id, coder_id, codebook_used_id)
);
-- Collapse duplicates that previously differed only on research_context_used_id
-- by keeping the most recently enqueued row per (segment, coder, codebook).
INSERT INTO coding_queue__new
    (segment_id, coder_id, codebook_used_id, enqueued_at, claimed_at, finished_at, error)
SELECT q.segment_id, q.coder_id, q.codebook_used_id,
       q.enqueued_at, q.claimed_at, q.finished_at, q.error
FROM coding_queue q
JOIN (
    SELECT segment_id, coder_id, codebook_used_id, MAX(enqueued_at) AS max_enq
    FROM coding_queue
    GROUP BY segment_id, coder_id, codebook_used_id
) latest
  ON latest.segment_id = q.segment_id
 AND latest.coder_id   = q.coder_id
 AND latest.codebook_used_id = q.codebook_used_id
 AND latest.max_enq    = q.enqueued_at;
DROP TABLE coding_queue;
ALTER TABLE coding_queue__new RENAME TO coding_queue;
CREATE INDEX ix_coding_queue_coder_id ON coding_queue(coder_id);

-- 4. Stage-2 raw-SQL tables: drop the now-redundant column.
ALTER TABLE theme_coder_runs   DROP COLUMN research_context_version;
ALTER TABLE theme_aggregations DROP COLUMN research_context_version;

PRAGMA foreign_keys = ON;
PRAGMA foreign_key_check;

COMMIT;
