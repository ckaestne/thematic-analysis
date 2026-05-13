# Stage 1 — incremental, SQLite-backed rewrite

> **Status:** historical design doc. The rewrite landed (Stage 1 + Stage 2),
> the old in-memory `ThematicLMPipeline` has been removed, and the remaining
> `thematic_analysis` package is now a library of agents, prompts, codebook
> data structures, loaders, and research-context primitives that this
> package consumes. See `README.md` for current usage.

A side-by-side reimplementation of the Stage 1 pipeline (Coder → Aggregator
→ Reviewer) using SQLite as the bus between actors.

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

-- Coders (registered via add-coder; identity is the persona shown to the agent).
coders(
  coder_id    TEXT PRIMARY KEY,
  identity    TEXT NOT NULL,
  created_at  TEXT NOT NULL
)

-- Input data.
segments(
  segment_id  TEXT PRIMARY KEY,
  text        TEXT NOT NULL,
  batch       INTEGER,                            -- enqueue grouping
  status      TEXT NOT NULL DEFAULT 'pending'    -- pending|coding|aggregating|reviewing|done
)

-- One row per (segment, coder). Created lazily when the coder starts
-- working on the segment — there is no `pending` state.
coder_runs(
  id                INTEGER PRIMARY KEY,
  segment_id        TEXT NOT NULL REFERENCES segments,
  coder_id          TEXT NOT NULL REFERENCES coders,
  codebook_version  INTEGER NOT NULL REFERENCES codebook_versions,
  status            TEXT NOT NULL DEFAULT 'running',  -- running|done|failed
  claimed_at        TEXT,
  finished_at       TEXT,
  raw_response      TEXT,                              -- full LLM output (debug)
  error             TEXT,
  UNIQUE(segment_id, coder_id)
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
  source_coders_json  TEXT NOT NULL                   -- list of coder_id
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
pending → coding (a coder_run row was inserted for this segment)
       → aggregating (every registered coder has a 'done' coder_run; aggregator
                      decides this at query time, based on `coders` ⨝ `coder_runs`)
       → reviewing  (aggregation done; some review_decisions pending)
       → done       (all review_decisions applied)
```

`segments.status` advances to `coding` automatically when the first
coder_run is opened. Later transitions are written by the aggregator and
reviewer steps respectively.

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
      segments:         120 total | pending=12 | coding=44 | aggregating=8 | done=56
      coders:           3
      coder_runs:       360 total | running=2 | done=352 | failed=6
      aggregations:     78 total  | done=74 | failed=4
      review_decisions: 412 total | applied=412
      codebook:         v=37 codes=84

ta-stage1 export-codebook --db x.sqlite [--version N] -o codebook.json
    Dump the JSON snapshot of the chosen version (default: latest).
```

### LLM environment

`CoderAgent` (and friends) use `openhands.sdk.LLM.load_from_env()`,
which reads env vars with the `LLM_` prefix mapped to LLM model fields
— **not** the `LITELLM_*` / `ANTHROPIC_API_KEY` names. To run real
coders set at least:

```
LLM_MODEL=anthropic/claude-sonnet-4-20250514
LLM_API_KEY=...
# optional: LLM_BASE_URL=...
```

## Implementation steps

Each step is independently shippable. After each, `pytest` should pass and
`ta-stage1 status` should reflect reality.

### Step 1 — Schema, DAL, and bookkeeping commands  ✅ done
- `schema.py`: idempotent `create_schema(conn)` (also called from
  `connect()`, so any subcommand can run against a fresh or partially-
  migrated DB without an explicit `init`).
- `store.py`: DAL wrappers — `init_db`, `latest_codebook_version`,
  `get_codebook_version`, `insert_codebook_version`, `add_coder`,
  `remove_coder`, `get_coder`, `list_coders`, `enqueue_segments`
  (segments only — no implicit coder_runs), `segments_to_code`,
  `start_coder_run`, `record_coder_result`, `record_coder_failure`,
  `reset_unfinished_coder_runs`, `status_counts`.
- `cli.py`: `init`, `add-coder`, `rm-coder`, `list-coders`, `enqueue`,
  `add-document`, `status`, `export-codebook`.
- `pyproject.toml`: registered `ta-stage1`.
- Tests cover schema idempotence, segment idempotence, coder
  add/remove (refused while runs exist), `segments_to_code` exclusion
  semantics, and CLI flows.

### Step 2 — Coder worker  ✅ done
- `workers.code_one(conn, coder_id)` and `code_one_async`: pick the
  next segment without a `coder_runs` row for this coder, insert a
  `running` row pinned to the latest codebook version, instantiate
  `CoderAgent` with `CoderConfig(identity=coder.identity)`, run, then
  record `done` (with `coder_codes`) or `failed`.
- Per-process Codebook cache keyed on `version` (avoids re-embedding
  across consecutive same-version tasks). `clear_codebook_cache()`
  available for tests / version bumps.
- `workers.drain_code_async`: K concurrent coroutines, optional
  `limit`, calls a per-result `on_event` callback for progress lines.
- `cli.py code <coder_id> [--limit N] [--workers K] [--retry-failed]
  [--mock-embeddings]`. `--retry-failed` deletes `failed`/`running`
  rows for the coder so they get re-coded; `--mock-embeddings` skips
  the real sentence-transformer load (testing / no-network).
- Tests: stub-agent factory exercises happy path, failure path,
  retry-after-clear, and two-coders-independent. CLI test
  monkeypatches `workers.default_coder_factory`.

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
