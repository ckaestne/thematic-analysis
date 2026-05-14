# Thematic Analysis

An LLM-powered tool for automated thematic analysis of qualitative data.
Analyse PDFs, text files, or raw text to discover codes and themes.

Built with the [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk).

## Installation

```bash
git clone https://github.com/ckaestne/thematic-analysis.git
cd thematic-analysis
pip install -e .
```

`pip install -e .` registers the `ta-stage1` and `ta-stage2` console scripts
into your active Python environment.

```bash
ta-stage1 --help
ta-stage2 --help
```

## Environment Variables

The agents call an LLM via the OpenHands SDK. Set at minimum:

```bash
export LLM_MODEL=anthropic/claude-sonnet-4-6   # or any supported model
export LLM_API_KEY=your-api-key
# optional: export LLM_BASE_URL=https://...
```

## Pipeline (`ta-stage1` / `ta-stage2`)

A SQLite-backed pipeline that stores every intermediate result so each step
can be run separately, resumed after failure, and rerun without redoing
finished work.

| Command | Stage |
|---|---|
| `ta-stage1` | Coding → Aggregation → Review → Codebook |
| `ta-stage2` | Theme coding → Theme aggregation → Final themes |

Both commands share the same SQLite file.

See [`src/thematic_analysis_inc/README.md`](src/thematic_analysis_inc/README.md)
for the full usage guide, including all CLI flags, the database schema, and
the adversarial code-refinement loop.

## Web inspector (`ta-web`)

A React + FastAPI UI for inspecting and editing pipeline state. Install the
optional `web` extras, then point it at any analysis database:

```bash
pip install -e '.[web]'
ta-web --db analysis.sqlite
# → serving analysis.sqlite at http://127.0.0.1:8765
```

The UI shows progress at every stage (segments → coder runs → aggregation →
review → codebook → theme coders → themes), lets you edit code text, and lets
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
4. **Theme Coders** — group codes into higher-level themes.
5. **Theme Aggregator** — produces the final consolidated themes.

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
