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
Append-only history of research-context revisions. Each call to
`set_research_context` inserts a new row **and** creates a fresh
`codebook` revision pinned to it; the row with the highest
`research_context_version` is the "current" one. Downstream tables
only carry `codebook_version` — the matching research context is
`codebook.research_context_version` of the referenced codebook (see
[Versioning of research context](#versioning-of-research-context)).

```
research_context(
    research_context_version  INTEGER PK AUTOINCREMENT,
    description               TEXT NOT NULL DEFAULT '',
    coder_prompt              TEXT,
    coding_critic_prompt      TEXT,
    reviewer_prompt           TEXT,
    theme_coder_prompt        TEXT,
    theme_aggregator_prompt   TEXT,
    created_at                DATETIME NOT NULL
)
```

The five `*_prompt` columns map to the role names exposed by
`thematic_analysis.research_context.AGENT_ROLES`:
`coder`, `coding_critic`, `reviewer`, `theme_coder`, `theme_aggregator`.
A `NULL` value means "no tailored prompt set for this role"; the agent
falls back to the freeform `description`.

`clear_research_context` deletes **all** rows (fresh-DB policy).

### codebook_versions
Append-only history of codebook revisions. Membership is stored in a
separate `codebook` table; this table only carries metadata. Each
revision pins the `research_context` revision it was authored against,
so the rest of the pipeline only tracks `codebook_version`.

```
codebook_versions(
    version                   INTEGER PK,
    parent_version            INTEGER REFERENCES codebook_versions(version),
    research_context_version  INTEGER NOT NULL REFERENCES research_context(research_context_version),
    created_at                DATETIME NOT NULL
)
```

Version 1 is created during `init_db` as the side effect of seeding the
empty research context. Subsequent revisions come from either (a) a
`set_research_context` call (new RC → new codebook pinned to it) or
(b) a non-SKIP reviewer decision.

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
    created_at  DATETIME NOT NULL
)
```

### documents

```
documents(
    document_id  INTEGER PK AUTOINCREMENT,
    filename     TEXT NOT NULL,
    content      BLOB NOT NULL,
    created_at   DATETIME NOT NULL
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

`codebook_version` is the codebook version active when the code was
authored. The research context active at authoring time is reachable
via that codebook revision (`codebook.research_context_version`).

```
codes(
    code_id           INTEGER PK AUTOINCREMENT,
    segment_id        INTEGER REFERENCES segments(segment_id),
    coder_id          INTEGER NOT NULL REFERENCES coders(coder_id),
    codebook_version  INTEGER REFERENCES codebook_versions(version),
    code              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    rationale         TEXT NOT NULL DEFAULT ''
)
```

Indexes: `(segment_id)`, `(coder_id)`, `(codebook_version)`.

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
    claimed_at        DATETIME,
    finished_at       DATETIME,
    error             TEXT,
    PRIMARY KEY (segment_id, coder_id, codebook_version)
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

## Stage 2 — theme tables

The research context active for a Stage-2 row is reachable via its
`codebook_version` (which pins one):

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

## Datetime columns

Every timestamp column above is declared as `DATETIME` and stored as an
**ISO 8601** UTC string (e.g. `2024-08-22T14:32:01+00:00`). The declared
type is purely documentation: the connection layer deliberately does *not*
enable `sqlite3.PARSE_DECLTYPES`, so reads return plain strings, matching
all existing comparison sites (`finished_at IS NOT NULL`, string equality
in tests, JSON serialisation, …). Writes use
`datetime.now(timezone.utc).isoformat(timespec="seconds")`.

## Versioning of research context

The research context evolves over time; each `set_research_context`
call creates a new row in `research_context` with a fresh
`research_context_version` **and** a new `codebook` revision pinned to
it (membership copied from the previous codebook). Downstream tables —
`codes`, `coding_queue`, `theme_coder_runs`, `theme_aggregations` —
only carry `codebook_version`; the research context for any row is
`codebook.research_context_version` of the referenced codebook.

---

Segment-level derived status (used by status payloads):

| condition                                                        | status        |
|------------------------------------------------------------------|---------------|
| any coding_queue row has `error IS NOT NULL`                     | failed        |
| any coding_queue row has `finished_at IS NULL AND claimed_at IS NOT NULL` | coding |
| any coding_queue row has `claimed_at IS NULL`                    | pending       |
| no aggregator code (`coder_id=0`) yet                            | aggregating   |
| aggregator codes exist with un-reviewed entries                  | reviewing     |
| otherwise                                                        | done          |
