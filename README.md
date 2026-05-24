# Thematic Analysis

An LLM-powered tool for automated thematic analysis of qualitative data.
Analyse PDFs, text files, or raw text to discover codes and themes.

Built with the [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk).

## Installation

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
git clone https://github.com/ckaestne/thematic-analysis.git
cd thematic-analysis
uv sync
```

`uv sync` creates a `.venv/` and installs the project plus its dependencies.
Run the console scripts via `uv run`:

```bash
uv run ta --help
```

## Environment Variables

The agents call an LLM via the OpenHands SDK. Set at minimum:

```bash
export LLM_MODEL=anthropic/claude-sonnet-4-6   # or any supported model
export LLM_API_KEY=your-api-key
# optional: export LLM_BASE_URL=https://...
```

### Per-task model overrides

Each LLM-using task can be pointed at a different model / temperature /
max output tokens. Task-specific vars override the global `LLM_MODEL` etc.;
values explicitly passed in code or via the CLI still win over env.

```bash
# pick a stronger model for coding, a cheaper one for aggregation
export LLM_MODEL_CODER=anthropic/claude-opus-4-7
export LLM_MODEL_AGGREGATOR=anthropic/claude-haiku-4-5
export LLM_TEMPERATURE_REVIEWER=0.2
export LLM_MAX_TOKENS_THEME_CODER=8192
export LLM_MODEL_SEGMENTER=gemini/gemini-2.5-pro
export LLM_MODEL_TAILOR=anthropic/claude-sonnet-4-6
```

Recognised task names: `CODER`, `AGGREGATOR`, `REVIEWER`, `THEME_CODER`,
`SEGMENTER`, `TAILOR` (research-context prompt generation).

## Pipeline (`ta`)

A SQLite-backed pipeline that stores every intermediate result so each step
can be run separately, resumed after failure, and rerun without redoing
finished work. All operations are exposed as subcommands of a single `ta`
entry point sharing one SQLite file:

| Subcommand group | Stage |
|---|---|
| `code`, `aggregate`, `review`, `status` | Stage 1: Coding → Aggregation → Review → Codebook |

Stage 2 (theme development) is being rewritten and currently has no
subcommands.

Run `ta --help` to see every subcommand. Pass `--debug` for full
tracebacks on unexpected errors.

See [`src/thematic_analysis_inc/README.md`](src/thematic_analysis_inc/README.md)
for the full usage guide, including all CLI flags, the database schema, and
the adversarial code-refinement loop.

## Web inspector (`ta-web`)

A React + FastAPI UI for inspecting and editing pipeline state. Point it at
any analysis database:

```bash
uv run ta-web --db analysis.sqlite
# → serving analysis.sqlite at http://127.0.0.1:8765
```

The UI shows progress at every stage (segments → coder runs → aggregation →
review → codebook), lets you edit code text, and lets
you delete results so the pipeline re-computes them on the next worker run.

For frontend development, run `npm install && npm run dev` inside `frontend/`
(it proxies `/api` to `127.0.0.1:8765`). The production build outputs to
`src/thematic_analysis_inc/web_static/`, which `ta-web` serves directly.

## Supported File Formats

- **PDF** (`.pdf`) — automatic text extraction
- **Text** (`.txt`) — plain text files
- **Markdown** (`.md`) — markdown files

## How It Works

The pipeline uses a multi-agent architecture:

1. **Coders** — multiple independent agents analyse text segments and assign codes.
2. **Aggregator** — merges similar codes from different coders.
3. **Reviewer** — validates each aggregated code and updates the versioned codebook.

Stage 2 (theme development) is being rewritten.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Acknowledgments

This project is inspired by and based on concepts from:

> Qiao, T., Walker, C., Cunningham, C., & Koh, Y. S. (2025). **Thematic-LM: A LLM-based Multi-agent System for Large-scale Thematic Analysis**. In *Proceedings of the ACM Web Conference 2025 (WWW '25)*. https://doi.org/10.1145/3696410.3714595

```bibtex
@inproceedings{qiao2025thematiclm,
  title={Thematic-LM: A LLM-based Multi-agent System for Large-scale Thematic Analysis},
  author={Qiao, Tingrui and Walker, Caroline and Cunningham, Chris and Koh, Yun Sing},
  booktitle={Proceedings of the ACM Web Conference 2025 (WWW '25)},
  year={2025},
  doi={10.1145/3696410.3714595}
}
```
