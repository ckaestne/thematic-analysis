# Agent Instructions

## Designing or modifying agents

When adding a new agent role (Coder, Aggregator, Reviewer, ThemeCoder,
ThemeAggregator, …), changing an agent's public method, or wiring an
agent into the worker layer, follow the conventions in
[`.claude/skills/agent-design/SKILL.md`](.claude/skills/agent-design/SKILL.md):
agents take and return SQLModel objects directly, navigate relationships
instead of carrying parallel data, never touch the DB, and own their
sentinel and short-circuit cases.

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
