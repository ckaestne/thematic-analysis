# Stage 1 / Stage 2 SQLite schema

This is the authoritative reference for the Stage 1 + Stage 2 SQLite
schema. The DDL itself lives in `db/schema.py`; all helpers that read or
write these tables live in `db/`. Nothing outside the `db/` package
should contain raw SQL.

The pipeline is **incremental and resumable**: each run is recorded, and
status is derived from row presence rather than stored explicitly. Where
status columns remain (Stage 2 `theme_*`), they're carried over verbatim
from the previous schema.

---

## Stage 1 — core tables

### research_context
Singleton (id=1) row carrying the freeform research context + the
per-role tailored prompts.

```
research_context(
    id           INTEGER PK CHECK (id = 1),
    context_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL
)
```

### codebook_versions
Append-only history of codebook revisions. Membership is stored in a
separate `codebook` table; this table only carries metadata.

```
codebook_versions(
    version         INTEGER PK,
    parent_version  INTEGER REFERENCES codebook_versions(version),
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL
)
```

Version 1 is created during `init_db` with no parent.

### coders
Coder is now an integer-keyed table. Two **reserved** rows are seeded
during schema creation:

| coder_id | role        |
|---------:|-------------|
|        0 | aggregator  |
|       -1 | reviewer    |

User-supplied coders are auto-assigned ids ≥ 1.

```
coders(
    coder_id    INTEGER PK,
    name        TEXT NOT NULL,
    identity    TEXT NOT NULL,
    created_at  TEXT NOT NULL
)
```

### documents

```
documents(
    document_id  INTEGER PK AUTOINCREMENT,
    filename     TEXT NOT NULL,
    content      BLOB NOT NULL,
    created_at   TEXT NOT NULL
)
```

### segments
Segment ids are now INTEGER (auto-assigned).

```
segments(
    segment_id   INTEGER PK AUTOINCREMENT,
    document_id  INTEGER REFERENCES documents(document_id),
    content      TEXT NOT NULL,
    line_from    INTEGER,
    line_to      INTEGER,
    position     INTEGER
)
```

### codes
Every code (Stage-A coder code, aggregator code, reviewer code) lands in
this table. The author is identified by `coder_id`:

- `coder_id ≥ 1` — Stage-A coder output
- `coder_id = 0` — aggregator output
- `coder_id = -1` — reviewer output (the canonical codebook code)

`version` is the codebook version active when the code was authored.

```
codes(
    code_id      INTEGER PK AUTOINCREMENT,
    segment_id   INTEGER REFERENCES segments(segment_id),
    coder_id     INTEGER NOT NULL REFERENCES coders(coder_id),
    version      INTEGER REFERENCES codebook_versions(version),
    code         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    rationale    TEXT NOT NULL DEFAULT ''
)
```

Indexes: `(segment_id)`, `(coder_id)`, `(version)`.

Reviewer codes have `segment_id = NULL` (a reviewer code is not tied to a
specific segment).

### quotes

```
quotes(
    quote_id    INTEGER PK AUTOINCREMENT,
    segment_id  INTEGER REFERENCES segments(segment_id),
    text        TEXT NOT NULL
)
```

Indexes: `(segment_id)`.

### codes_supporting_quotes
Many-to-many: a code may be supported by several quotes; a quote may
support several codes.

```
codes_supporting_quotes(
    code_id   INTEGER NOT NULL REFERENCES codes(code_id),
    quote_id  INTEGER NOT NULL REFERENCES quotes(quote_id),
    PRIMARY KEY (code_id, quote_id)
)
```

### codes_derived
Provenance graph: this code derives from those codes. Two derivation
kinds — aggregation (`'A'`) and review (`'R'`).

```
codes_derived(
    new_code_id      INTEGER NOT NULL REFERENCES codes(code_id),
    source_code_id   INTEGER NOT NULL REFERENCES codes(code_id),
    derivation_type  TEXT NOT NULL CHECK (derivation_type IN ('A','R')),
    decision         TEXT CHECK (decision IS NULL OR decision IN ('A','M','U')),
    rationale        TEXT,
    PRIMARY KEY (new_code_id, source_code_id)
)
```

- `derivation_type = 'A'` — aggregation. One row per source coder code.
  `decision` is NULL.
- `derivation_type = 'R'` — review. One row per aggregator source code.
  `decision` is one of:
  - `'A'` — ADD (introduce a new code into the codebook)
  - `'M'` — MERGE (fold into an existing target code)
  - `'U'` — UPDATE (rename/replace an existing target code)

A SKIP review is **not** represented — it leaves no row at all in
`codes_derived`. "Skipped aggregator code" is detected as "an aggregator
code with no outgoing `derivation_type='R'` edge".

Indexes: `(source_code_id, derivation_type)`.

### codebook
Membership: which reviewer codes belong to which codebook version.

```
codebook(
    version  INTEGER NOT NULL REFERENCES codebook_versions(version),
    code_id  INTEGER NOT NULL REFERENCES codes(code_id),
    PRIMARY KEY (version, code_id)
)
```

Index: `(version)`.

### coding_queue
Replaces the old `coder_runs` table. One row per (segment, real coder)
at the chosen codebook version; status is *derived* from
`claimed_at`/`finished_at`/`error`.

```
coding_queue(
    segment_id        INTEGER NOT NULL REFERENCES segments(segment_id),
    coder_id          INTEGER NOT NULL REFERENCES coders(coder_id),
    codebook_version  INTEGER NOT NULL REFERENCES codebook_versions(version),
    claimed_at        TEXT,
    finished_at       TEXT,
    error             TEXT,
    PRIMARY KEY (segment_id, coder_id)
)
```

Derived status:

| condition                                  | status     |
|--------------------------------------------|------------|
| `error IS NOT NULL`                        | failed     |
| `finished_at IS NOT NULL`                  | done       |
| `claimed_at IS NOT NULL`                   | running    |
| otherwise                                  | pending    |

Index: `(coder_id)`.

---

## Stage 2 — theme tables (unchanged)

Stage 2 schema is carried over verbatim from the previous design:

- `theme_coders(theme_coder_id, identity, created_at)`
- `theme_coder_runs(id, theme_coder_id, codebook_version, status, claimed_at, finished_at, result_json, raw_response, error)`
- `theme_aggregations(id, codebook_version UNIQUE, status, created_at, finished_at, result_json, error)`
- `theme_aggregation_inputs(theme_aggregation_id, theme_coder_run_id)`

These keep their explicit `status` column; the surrounding pipeline
expectations are unchanged.

---

## Pipeline state machine

The four pipeline stages — coding, aggregation, review, codebook — are
encoded by the presence/absence of rows in the tables above:

1. **Coding** — for each (segment, coder ≥ 1), a `coding_queue` row is
   inserted (`sync_coding_queue`). Workers atomically claim a pending
   row (set `claimed_at`), run the coder agent, then either:
   - on success, insert `codes` rows (with `coder_id = that coder`,
     `description = ''`, `rationale = …`), and set `finished_at`;
   - on failure, set `error`.
2. **Aggregation** — for a segment whose every `coding_queue` row is
   `done` (no errors), and which has no `codes(coder_id=0, segment_id=…)`
   row yet, the aggregator agent runs. For every merged code it produces:
   - quotes are inserted into `quotes` (linked to the segment),
   - one new `codes` row is inserted with `coder_id = 0`,
   - quote links go into `codes_supporting_quotes`,
   - one `codes_derived('A', NULL, NULL)` edge is inserted per
     contributing coder code.
3. **Review** — for each aggregator code without an outgoing
   `codes_derived(..., derivation_type='R')` edge, the reviewer agent
   runs. On ADD/MERGE/UPDATE the reviewer:
   - inserts a new `codes` row with `coder_id = -1`,
   - inserts a `codes_derived('R', decision, rationale)` edge from
     the aggregator code to the new reviewer code,
   - inserts a new `codebook_versions` row (parent = previous version),
   - copies the previous version's `codebook` membership into the new
     version, with the appropriate ADD/MERGE/UPDATE adjustment
     (`MERGE` keeps the target; `UPDATE` replaces the target row's
     `code_id`; `ADD` appends).
   On SKIP, nothing is written.
4. **Codebook** — the canonical codebook is `codebook ⋈ codes` filtered
   by `version`. `get_codebook_codes(conn, version)` materialises this
   for callers, and `codebook_to_json_for_version` serialises it in the
   legacy `{"codes":[{code, quotes}]}` shape consumed by
   `Codebook.from_json`.

Segment-level derived status (used by status payloads):

| condition                                                        | status        |
|------------------------------------------------------------------|---------------|
| any coding_queue row has `error IS NOT NULL`                     | failed        |
| any coding_queue row has `finished_at IS NULL AND claimed_at IS NOT NULL` | coding |
| any coding_queue row has `claimed_at IS NULL`                    | pending       |
| no aggregator code (`coder_id=0`) yet                            | aggregating   |
| aggregator codes exist with un-reviewed entries                  | reviewing     |
| otherwise                                                        | done          |
