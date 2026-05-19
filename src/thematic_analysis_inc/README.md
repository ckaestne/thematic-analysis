# Incremental Thematic Analysis Pipeline

A SQLite-backed, step-by-step reimplementation of the thematic analysis
pipeline. Every intermediate result is stored in the database, so each step
can be run separately, resumed after failure, and rerun without redoing
finished work.

A single `ta` CLI exposes every step as a subcommand:

| Subcommand group | Stage |
|---|---|
| `code`, `aggregate`, `review` | Coding → Aggregation → Review → Codebook |
| `generate-themes` | Theme coding → Theme aggregation → Final themes |

All subcommands share the same SQLite file (`--db <path>`).

---

## Installation

```bash
git clone https://github.com/ckaestne/thematic-analysis.git
cd thematic-analysis
pip install -e .
```

`pip install -e .` registers the `ta` console script into your active
Python environment. After that you can call it directly:

```bash
ta --help
```

If the commands are not found, check that the environment's `bin/` (or
`Scripts/` on Windows) is on your `PATH`. With a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
ta --help
```

## Environment variables

The agents call an LLM via the OpenHands SDK. Set at minimum:

```bash
export LLM_MODEL=anthropic/claude-sonnet-4-6   # or any supported model
export LLM_API_KEY=your-api-key
# optional: export LLM_BASE_URL=https://...
```

Use `--mock-embeddings` on `code` and `aggregate` to skip the
sentence-transformer load during testing / offline work.

---

## Stage 1 — Coding, Aggregation, Review

### 1. Initialise

```bash
ta --db analysis.sqlite init
```

Creates the schema and an empty codebook (version 1). Safe to re-run on an
existing database.

### 2. Load data

```bash
# From .txt or .md files — auto-segmented (LLM by default)
ta --db analysis.sqlite add-document interviews/*.txt
```

`add-document` is idempotent: re-running it skips files whose filename is
already stored and segments whose `segment_id` already exists.

### 3. Register coders

Each coder gets an independent analytical perspective shown to the agent.

```bash
ta --db analysis.sqlite add-coder alice "feminist scholar"
ta --db analysis.sqlite add-coder bob   "grounded theory researcher"
ta --db analysis.sqlite list-coders
```

### 4. Code segments

Each coder works through all segments independently. Multiple coders can
run in parallel (even on different machines sharing the same DB file).

```bash
ta --db analysis.sqlite code alice --workers 4
ta --db analysis.sqlite code bob   --workers 4

# Retry any failed or stalled runs before starting
ta --db analysis.sqlite code alice --retry-failed

# Process only the next N segments
ta --db analysis.sqlite code alice --limit 10
```

Each segment goes through an adversarial refinement loop:

1. **Code** in the coder chat (full context: codebook, identity,
   research context, similar-codes hints).
2. **Critique** in a separate critic chat that sees the segment text,
   the codes the coder just produced, and (if set) the research
   context — the critic needs the research context to judge relevance.
   It does **not** see the codebook, the coder's identity, or
   similar-codes hints. The critic is told to push back on relevance
   to the research focus, shallow paraphrase vs analytic themes, and
   grounding — and to recommend dropping codes (or all codes) when
   the segment isn't actually about the research question.
3. **Refine** by returning to the coder chat, appending the critique
   as a user turn, and asking for a stronger code set. The coder may
   drop, rename, split, merge, or add codes; an empty `codes` list is
   a valid answer when nothing in the segment speaks to the research
   focus.

The coder chat reuses the same prefix (`system + initial user +
assistant`) for the refinement turn, so provider-side prompt caching
applies. If the first pass returns OUT_OF_SCOPE / no codes, both the
critic and refinement turns are skipped.

### 5. Update the codebook

Runs aggregation and review in sequence. Aggregation merges codes from all
coders for each segment; review then processes each aggregated code,
deciding whether to add it to the codebook, merge it with an existing code,
update an existing code, or skip it. Each decision that changes the
codebook creates a new versioned snapshot. Safe to restart —
already-processed segments and codes are skipped.

```bash
ta --db analysis.sqlite update-codebook

# Retry failed aggregations and continue
ta --db analysis.sqlite update-codebook --retry-failed

# Process only the next N items per stage
ta --db analysis.sqlite update-codebook --limit 20
```

### 6. Check progress

```bash
ta --db analysis.sqlite status
# segments:         120 total | done=118 reviewing=2
# coders:           2
# coder_runs:       240 total | done=238 failed=2
# aggregations:     120 total | done=120
# review_decisions: 487 total | applied=487
# codebook:         v=52 codes=41
```

### 7. Export the codebook

```bash
# Latest version (default)
ta --db analysis.sqlite export-codebook -o codebook.json

# A specific earlier version
ta --db analysis.sqlite export-codebook --version 30 -o codebook_v30.json
```

---

## Stage 2 — Theme Development

Stage 2 reads the codebook built by Stage 1 from the same database and
develops higher-level themes. Run it after Stage 1 `review` is complete.

### 0. (Recommended) Set the research context

Without a research context, theme coders and the aggregator have nothing
to anchor on and produce themes that drift away from the research
question. Set it once on the database — it is shared with Stage 1 too:

```bash
ta --db analysis.sqlite set-research-context \
    --aim "Understand how lay users justify climate-policy skepticism" \
    --research-question "What rhetorical strategies do skeptics use to justify inaction?" \
    --domain "climate change" \
    --theoretical-framework "critical discourse analysis"

ta --db analysis.sqlite show-research-context
```

You can also pass `--file context.json` with the same field names.

### 1. Register theme coders

```bash
ta --db analysis.sqlite add-theme-coder t1 "critical discourse analyst"
ta --db analysis.sqlite add-theme-coder t2 "phenomenological researcher"
ta --db analysis.sqlite list-theme-coders
```

### 2. Generate themes

Each theme coder independently reads the codebook and proposes a set of
themes; once all coders have finished, their themes are merged into a
final consolidated set. Multiple coders can run in parallel.

```bash
# Uses the latest codebook version by default
ta --db analysis.sqlite generate-themes --workers 2

# Pin to a specific codebook version
ta --db analysis.sqlite generate-themes --codebook-version 30

# Retry failed runs and aggregations
ta --db analysis.sqlite generate-themes --retry-failed
```

### 3. Check progress

```bash
ta --db analysis.sqlite theme-status
# codebook version:     v52
# theme_coders:         2
# theme_coder_runs:     2 total | done=2
# theme_aggregations:   1 total | done=1
# themes in result:     5
```

### 5. Export themes

```bash
ta --db analysis.sqlite export-themes -o themes.json

# From a specific codebook version
ta --db analysis.sqlite export-themes --codebook-version 30 -o themes_v30.json
```

For a human-readable, interactive report, export to HTML instead. The output is
a self-contained page (Bulma is loaded from CDN) with a live search filter and
collapsible theme cards — just open it in a browser:

```bash
ta --db analysis.sqlite export-themes-html -o themes.html
```

The output is a JSON object:

```json
{
  "themes": [
    {
      "name": "Identity and Power",
      "description": "...",
      "original_themes": ["Identity", "Power Dynamics"],
      "codes": ["code-a", "code-b", "code-c"],
      "quotes": [{"quote_id": "seg_0001", "text": "..."}],
      "merge_rationale": "Both themes describe..."
    }
  ]
}
```

---

## Database schema overview

See [`data-schema.md`](./data-schema.md) for the authoritative reference.
All SQL lives in the `db/` sub-package; nothing outside `db/` contains
raw SQL.

```
research_context    — singleton row with the freeform research context
codebook_versions   — append-only metadata; membership stored separately
coders              — INTEGER ids; rows 0 (aggregator) and -1 (reviewer) are seeded
documents           — uploaded source documents (BLOB content)
segments            — INTEGER-keyed text segments
codes               — every code (Stage-A, aggregator, reviewer); coder_id picks the kind
quotes              — quote text per segment
codes_supporting_quotes  — n:m link from codes to quotes
codes_derived       — provenance edges: 'A' = aggregation, 'R' = review (decision A/M/U)
codebook            — which reviewer codes belong to which codebook version
coding_queue        — replaces coder_runs; status derived from claimed_at/finished_at/error

theme_coders, theme_coder_runs, theme_aggregations, theme_aggregation_inputs
                    — Stage 2 (unchanged)
```

---

## Full command reference

```
ta --db DB init
ta --db DB add-coder       ID IDENTITY
ta --db DB rm-coder        ID [--force]
ta --db DB list-coders
ta --db DB add-document    FILES... [--segmentation llm|paragraph|sentence|fixed]
                                           [--min-words N] [--max-words N] [--batch N]
ta --db DB code            ID [--workers K] [--limit N] [--retry-failed]
                                     [--mock-embeddings]
ta --db DB update-codebook [--limit N] [--retry-failed] [--mock-embeddings]
ta --db DB status
ta --db DB export-codebook [--version N] [-o FILE]

ta --db DB add-theme-coder   ID IDENTITY
ta --db DB rm-theme-coder    ID [--force]
ta --db DB list-theme-coders
ta --db DB generate-themes   [--codebook-version N] [--workers K] [--limit N]
                                    [--retry-failed] [--mock-embeddings]
ta --db DB theme-status      [--codebook-version N]
ta --db DB export-themes        [--codebook-version N] [-o FILE]
ta --db DB export-themes-html   [--codebook-version N] [-o FILE]
```
