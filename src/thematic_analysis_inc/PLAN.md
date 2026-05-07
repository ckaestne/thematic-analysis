# Stage 1 — incremental, SQLite-backed rewrite

A side-by-side reimplementation of the Stage 1 pipeline (Coder → Aggregator
→ Reviewer) using SQLite as the bus between actors. The existing
`thematic_analysis` package stays untouched.

## Goals

- **Decoupled actors**: Coder, Aggregator, Reviewer communicate only through
  the database. No shared Python state.
- **Restartable**: kill any worker mid-run, re-run, work resumes from the
  DB. No lost progress, no re-coding finished segments.
- **Observable**: every command prints meaningful progress; `status` shows
  pipeline health at a glance.
- **Run separately**: each step is its own subcommand. They can be run on
  the same machine or different ones (single shared DB file).

Stage 2 is out of scope for this rewrite — the existing `pipeline.py` Stage 2
continues to work and can consume the codebook produced here.

## Design decisions (already settled)

1. **Codebook persistence**: full JSON blob per version, append-only
   `codebook_versions` table. `Codebook.to_dict` / `from_dict` already
   round-trip; `from_dict` recomputes embeddings on load.
2. **Embeddings**: not stored. Recomputed by any worker that needs
   `find_similar_codes` (Coder, Reviewer). Per-process LRU keyed on
   `version` avoids re-embedding when consecutive tasks share a version.
   Aggregator skips loading the codebook entirely.
3. **Coders are first-class rows**: each coder has an `id` and an
   `identity` string (the persona shown to the agent). Coders are added /
   removed via `add-coder` / `rm-coder`. There is no implicit "N coders"
   parameter — coding work for a coder is the set of segments that don't
   yet have a `(segment_id, coder_id)` row in `coder_runs`.
4. **No pre-enqueued runs**: `coder_runs` rows are created when work
   actually starts — `code <coder_id>` walks segments missing a row for
   that coder, inserts a `running` row pinned to the latest codebook
   version, calls the LLM, and updates to `done` / `failed`.
5. **Reviewer is single-writer**: the `review` command holds an exclusive
   transaction when bumping the codebook version. Coders + aggregator can
   run in parallel.
6. **Reuse existing agent classes**: `CoderAgent`, `CodeAggregatorAgent`,
   `ReviewerAgent` from `thematic_analysis.agents` provide the LLM-call
   layer. The new workers instantiate them per task with a freshly-loaded
   `Codebook`. No prompt or LLM behavior changes.
7. **Layout**: everything new lives under
   `src/thematic_analysis_inc/`. The old package is not modified.

## Package layout

```
src/thematic_analysis_inc/
  __init__.py
  PLAN.md                  -- this file
  schema.py                -- DDL + idempotent migration
  store.py                 -- DAL: claim/insert/version ops
  workers.py               -- code_one(), aggregate_one(), review_one()
  cli.py                   -- single argparse entry point
  tests/                   -- end-to-end with mock embeddings + stub LLM
```

CLI entry registered in `pyproject.toml [project.scripts]`:
`ta-stage1 = "thematic_analysis_inc.cli:main"`.

## Database schema

```sql
-- Append-only codebook history. JSON blob is the source of truth.
codebook_versions(
  version         INTEGER PRIMARY KEY,           -- monotonic, starts at 1
  parent_version  INTEGER,                       -- nullable (v1 has none)
  snapshot_json   TEXT NOT NULL,                 -- Codebook.to_json()
  created_by      TEXT,                          -- 'init'|'reviewer'|'manual'
  created_at      TEXT NOT NULL                  -- ISO timestamp
)

-- Input data.
segments(
  segment_id  TEXT PRIMARY KEY,
  text        TEXT NOT NULL,
  batch       INTEGER,                            -- enqueue grouping
  status      TEXT NOT NULL DEFAULT 'pending'    -- pending|coding|aggregating|reviewing|done
)

-- One row per (coder_idx, segment).
coder_runs(
  id                INTEGER PRIMARY KEY,
  segment_id        TEXT NOT NULL REFERENCES segments,
  coder_idx         INTEGER NOT NULL,
  codebook_version  INTEGER NOT NULL REFERENCES codebook_versions,
  status            TEXT NOT NULL DEFAULT 'pending',  -- pending|claimed|done|failed
  claimed_at        TEXT,
  finished_at       TEXT,
  raw_response      TEXT,                              -- full LLM output (debug)
  error             TEXT,
  UNIQUE(segment_id, coder_idx)
)
coder_codes(
  coder_run_id  INTEGER NOT NULL REFERENCES coder_runs,
  position      INTEGER NOT NULL,
  code          TEXT NOT NULL,
  rationale     TEXT,
  is_new        INTEGER,                              -- 0/1
  PRIMARY KEY(coder_run_id, position)
)

-- One row per segment, created when all its coder_runs are done.
aggregations(
  id           INTEGER PRIMARY KEY,
  segment_id   TEXT NOT NULL UNIQUE REFERENCES segments,
  status       TEXT NOT NULL DEFAULT 'pending',      -- pending|done|failed
  created_at   TEXT NOT NULL,
  finished_at  TEXT,
  error        TEXT
)
aggregated_codes(
  id                  INTEGER PRIMARY KEY,
  aggregation_id      INTEGER NOT NULL REFERENCES aggregations,
  code                TEXT NOT NULL,
  quotes_json         TEXT NOT NULL,                  -- list of Quote dicts
  source_coders_json  TEXT NOT NULL                   -- list of coder_idx
)

-- One decision per aggregated_code.
review_decisions(
  id                   INTEGER PRIMARY KEY,
  aggregated_code_id   INTEGER NOT NULL UNIQUE REFERENCES aggregated_codes,
  decision             TEXT NOT NULL,                 -- add_new|merge|update|skip
  target_code          TEXT,
  rationale            TEXT,
  applied              INTEGER NOT NULL DEFAULT 0,    -- 0/1
  resulting_version    INTEGER REFERENCES codebook_versions,
  created_at           TEXT NOT NULL
)
```

State machine for a segment:
```
pending → coding (≥1 coder_run claimed)
       → aggregating (all coder_runs done; awaiting aggregation)
       → reviewing  (aggregation done; some review_decisions pending)
       → done       (all review_decisions applied)
```

## CLI surface

Single entry point `ta-stage1`. Every long-running command prints one line
per finished unit and a final summary.

```
ta-stage1 init       --db x.sqlite
    Create schema, write codebook v1 (empty Codebook).

ta-stage1 add-coder    --db x.sqlite <coder_id> <identity>
ta-stage1 rm-coder     --db x.sqlite <coder_id>
ta-stage1 list-coders  --db x.sqlite

ta-stage1 enqueue       --db x.sqlite --segments f.json [--batch N]
ta-stage1 add-document  --db x.sqlite FILES...
    Insert segments only (idempotent on segment_id). Coder_runs are
    created lazily when `code` runs.

ta-stage1 code       --db x.sqlite <coder_id> [--limit N] [--workers K] [--retry-failed]
    For the given coder, walk segments lacking a coder_run row, insert
    a 'running' row pinned to the latest codebook version, call the
    LLM, persist 'done' / 'failed'. Per-task line:
      [code] seg_0042 coder=alice codes=4 v=12 (37/120 ok=37 failed=0 3.1s)

ta-stage1 aggregate  --db x.sqlite [--limit N]
    For each segment with all coder_runs done and no aggregation row,
    merge codes. Per-task line:
      [aggregate] seg_0042 in=12 merged=7 retained=2 (19/40, 0.4s)

ta-stage1 review     --db x.sqlite [--limit N]
    For each aggregated_code without a review_decisions row, decide and
    apply. Bumps codebook version on add_new/merge/update. Per-task line:
      [review] code='peer support' decision=merge target='emotional support' v=12→13

ta-stage1 run        --db x.sqlite [--workers K]
    Loop code → aggregate → review until no work remains. Same per-line
    progress as the individual commands, prefixed by phase.

ta-stage1 status     --db x.sqlite
    Print:
      segments:    120 total | 12 pending | 14 coding | 30 aggregating | 8 reviewing | 56 done
      coder_runs:  360 total | 42 pending | 6 failed
      aggregations: 78 total | 4 failed
      reviews:     412 total | 412 applied
      codebook:    v=37 codes=84

ta-stage1 export-codebook --db x.sqlite [--version N] -o codebook.json
    Dump the JSON snapshot of the chosen version (default: latest).
```

## Implementation steps

Each step is independently shippable. After each, `pytest` should pass and
`ta-stage1 status` should reflect reality.

### Step 1 — Schema, DAL, and read-only commands
- `schema.py`: idempotent `create_schema(conn)`; `current_version(conn)`.
- `store.py`: thin DAL wrappers (no business logic):
  - `init_db(path)`: open connection with WAL + foreign_keys ON; create schema; write codebook v1 if missing.
  - `latest_codebook_version(conn) -> (version, snapshot_json)`.
  - `insert_codebook_version(conn, snapshot_json, parent, created_by) -> version`.
  - `enqueue_segments(conn, segments, num_coders, batch=None)`.
  - `claim_next_coder_run(conn) -> row|None` (atomic UPDATE…RETURNING).
  - `record_coder_result(conn, run_id, codes, raw_response)`.
  - `record_coder_failure(conn, run_id, error)`.
  - `next_segment_to_aggregate(conn) -> segment_id|None`.
  - `record_aggregation(conn, segment_id, merged, retained)`.
  - `next_aggregated_code_to_review(conn) -> row|None`.
  - `record_review_decision(conn, agg_code_id, decision, target, rationale, new_version)`.
  - `status_counts(conn) -> dict`.
- `cli.py`: `init`, `enqueue`, `status`, `export-codebook`.
- Register `ta-stage1` script in `pyproject.toml`.
- Tests: schema is idempotent; enqueue is idempotent on `segment_id`;
  `status` numbers match what was inserted.

### Step 2 — Coder worker
- `workers.code_one(conn) -> bool` — claim → load codebook for the pinned
  version → instantiate `CoderAgent` → call → record result. Returns
  `False` when nothing to claim.
- LRU-cache `Codebook.from_dict(snapshot_json)` keyed on version (per-
  process).
- `cli.py`: `code` subcommand drains in a loop with progress lines and
  summary. `--workers K` uses `asyncio.gather` over `code_segment_async`.
- Tests: with a stub LLM, a few segments produce the expected
  `coder_codes` rows; failures are recorded and retryable.

### Step 3 — Aggregator worker
- `workers.aggregate_one(conn) -> bool` — find a segment whose coder_runs
  are all `done` and which has no aggregation row; load its
  `CodeAssignment`s; run `CodeAggregatorAgent.aggregate`; persist
  `aggregations` + `aggregated_codes`.
- Aggregator instantiated with an empty `Codebook` (it doesn't need
  embeddings of the canonical codebook — its merging is local to the
  segment's coder outputs; verify against the existing implementation
  before locking that in, fall back to the latest snapshot if needed).
- Tests: stub coder outputs → expected merged/retained codes persisted.

### Step 4 — Reviewer worker
- `workers.review_one(conn) -> bool` — single-writer; opens an
  `IMMEDIATE` transaction. Pick next un-reviewed `aggregated_codes` row;
  load latest codebook version; run `ReviewerAgent.review_code`; apply
  decision in-memory; serialize to a new version; record decision with
  `resulting_version` and `applied=1`. Commit.
- After all of a segment's aggregated_codes are reviewed, mark
  `segments.status='done'`.
- Tests: each decision type produces the expected codebook diff and a new
  version row; SKIP creates no new version.

### Step 5 — `run` orchestrator
- Loop: drain `code` (parallel), then `aggregate`, then `review`, repeat
  until all three are idle. Phase-prefixed progress lines.
- Tests: end-to-end on a tiny corpus with stub LLM produces a final
  codebook and `status` shows everything `done`.

### Step 6 — End-to-end test + docs
- A pytest that calls `init` → `enqueue` → `run` on ~5 segments using mock
  embeddings + a stub LLM, asserts the final codebook JSON and that the
  pipeline is fully `done`.
- Short `README.md` in `thematic_analysis_inc/` covering CLI usage,
  the DB schema diagram, and how to point Stage 2 of the existing
  pipeline at an exported codebook.

## Out of scope (for now)

- Stage 2 (theme development) — keep using `pipeline.py`.
- Multi-machine coordination beyond a shared SQLite file (no Postgres,
  no job queue).
- HITL checkpoints (the existing `HITLCheckpoint` mechanism is not
  ported).
- Iterative pipeline / negotiation strategies beyond what
  `CodeAggregatorAgent` already implements.
- Evaluation hooks.

These can be layered on later — the schema leaves room (e.g. a
`checkpoints` table, an `iteration` column on `segments`).
