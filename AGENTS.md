# Agent Instructions

## Dependency management & running

This project uses [uv](https://docs.astral.sh/uv/). Do **not** use `pip`,
`python -m venv`, `python setup.py`, or invoke `python`/scripts directly.

- Install / sync deps: `uv sync` (add `--extra web` for the web inspector).
- Add a dependency: `uv add <pkg>` (use `uv add --dev <pkg>` for dev-only,
  or edit `pyproject.toml` then `uv sync`).
- Remove: `uv remove <pkg>`.
- Run any project command via `uv run`:
  - `uv run ta-stage1 ...`
  - `uv run ta-stage2 ...`
  - `uv run ta-web --db analysis.sqlite`
  - `uv run pytest`
  - `uv run python -c "..."` for ad-hoc scripts.
- The lockfile is `uv.lock` — commit it alongside `pyproject.toml` changes.
