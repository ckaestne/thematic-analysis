# Agent Instructions

## Dependency management & running

This project uses [uv](https://docs.astral.sh/uv/). Do **not** use `pip`,
`python -m venv`, `python setup.py`, or invoke `python`/scripts directly.

- Install / sync deps: `uv sync` (add `--extra web` for the web inspector).
- Add a dependency: `uv add <pkg>` (use `uv add --dev <pkg>` for dev-only,
  or edit `pyproject.toml` then `uv sync`).
- Remove: `uv remove <pkg>`.
- Run any project command via `uv run`:
  - `uv run ta ...`  (all Stage 1 + Stage 2 subcommands; see `ta --help`)
  - `uv run ta-web --db analysis.sqlite`
  - `uv run pytest`
  - `uv run python -c "..."` for ad-hoc scripts.
- The lockfile is `uv.lock` — commit it alongside `pyproject.toml` changes.

## Layering: db / worker / agent

The Stage-1 pipeline is intentionally split into three layers. Each
layer has one job, and the layer **above** it doesn't get to reach
past it.

```
agents/  ─────►  workers.py  ─────►  thematic_analysis_inc/db/
(LLM)            (orchestration)     (all SQL lives here)
```

1. **`thematic_analysis_inc/db/*.py` — the data layer.** Every
   `Session`, every `select(...)`, every `selectinload(...)`,
   every `s.add/s.merge/s.commit` lives in one of these modules.
   Functions accept fully-populated SQLModel objects and persist them
   in one transaction, or query rows and return them detached. They
   do **not** invent domain content (sentinel rows, default labels,
   fields the caller could have set). They *may* group multiple
   writes into one transaction; when they do, the function name has
   to say so out loud — e.g.
   `save_codes_and_finish_assignment`,
   `add_research_context_and_codebook_revision`,
   `apply_review_and_create_codebook_revision`. If a function does
   two things, both belong in the name.

2. **`workers.py` — orchestration.** Picks work off the queue, loads
   inputs through `db.*` helpers, hands them to an agent, persists
   the agent's output through `db.*` helpers, and shapes a result
   dict for the CLI/web layer. It is **forbidden** from importing
   `sqlmodel`, `sqlalchemy`, or `db.connection`; the `tests/
   test_layering.py` guard fails the build if any of those slip in.
   When the worker needs a paired load (segment + codebook in one
   session so the agent's `c.codebook_used is codebook` identity
   check holds — see `.claude/skills/sqlalchemy-identity`), the
   pair is loaded by a single helper such as
   `db.load_segment_and_codebook_for_aggregation`, never two
   independent `db.get_*` calls glued together in the worker.

3. **`thematic_analysis/agents/*.py` — the LLM layer.** Builds
   prompts, calls the model, parses responses. Returns SQLModel
   objects with every foreign-key column already set (`segment_id`,
   `coder_id`, `codebook_used_id`); the worker just `db.save_*`s
   them. Agents may import model classes from `db.models` and pure
   adapters (`db.research_context.to_domain`), but anything that
   opens a session or runs SQL is a hard no — same layering guard
   covers this.

**Why the rule has bite:** the data layer owns transactional
invariants (one codebook revision per research-context revision; a
finished coding assignment always has its codes persisted in the same
commit). The worker can't assemble those guarantees from primitives
without forgetting a step. The agent can't even try, because by the
time the agent has produced a result it has no session at all.

When you touch any of these three files, ask: "is this the layer
that owns this knowledge?" If the answer is "no", push it down (into
`db/`) or up (into the worker) until the answer is yes.

## Web UI

There are **two halves** of the web UI and any change must update both:

1. **Backend API** — `src/thematic_analysis_inc/web.py` (FastAPI app served by
   `uv run ta-web`).
2. **Frontend SPA** — `frontend/` (Vite + React + Mantine). Pages live under
   `frontend/src/pages/`, shared API client in `frontend/src/api.ts`,
   layout/components in `frontend/src/components/`.

The frontend is built into `src/thematic_analysis_inc/web_static/` and served
by the backend. **Whenever you change anything under `frontend/src/`, you
must rebuild**:

```
cd frontend && npm run build
```

Commit the rebuilt `web_static/` artifacts together with the source change so
the served UI stays in sync with the code.

When adding a new agent role, research-context field, or anything else that
appears in both the CLI and the API, also check whether the React UI needs
the same change — common spots:

- `frontend/src/pages/ResearchContextPage.tsx` — role list / labels
- `frontend/src/api.ts` — typed request/response shapes
- `frontend/src/pages/Overview.tsx` — pipeline counters
