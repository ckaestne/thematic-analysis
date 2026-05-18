# Incremental Thematic Analysis Pipeline

A SQLite-backed, step-by-step reimplementation of the thematic analysis
pipeline. Every intermediate result is stored in the database, so each step
can be run separately, resumed after failure, and rerun without redoing
finished work.

A single `ta` CLI exposes every step as a subcommand:

| Subcommand group | Stage |
|---|---|
| `code`, `aggregate`, `review` | Coding → Aggregation → Review → Codebook |
| `theme-code`, `theme-aggregate` | Theme coding → Theme aggregation → Final themes |

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

### 5. Aggregate codes

Merges codes from all coders for each segment. Runs serially; safe to
restart — already-aggregated segments are skipped.

```bash
ta --db analysis.sqlite aggregate

# Retry failed aggregations
ta --db analysis.sqlite aggregate --retry-failed
```

### 6. Review and build the codebook

The reviewer processes each aggregated code one at a time, deciding whether
to add it to the codebook, merge it with an existing code, update an
existing code, or skip it. Each decision that changes the codebook creates
a new versioned snapshot.

```bash
ta --db analysis.sqlite review

# Process only the next N codes
ta --db analysis.sqlite review --limit 20
```

### 7. Check progress

```bash
ta --db analysis.sqlite status
# segments:         120 total | done=118 reviewing=2
# coders:           2
# coder_runs:       240 total | done=238 failed=2
# aggregations:     120 total | done=120
# review_decisions: 487 total | applied=487
# codebook:         v=52 codes=41
```

### 8. Export the codebook

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

### 2. Develop themes

Each theme coder independently reads the codebook and proposes a set of
themes. Multiple coders can run in parallel.

```bash
# Uses the latest codebook version by default
ta --db analysis.sqlite theme-code --workers 2

# Pin to a specific codebook version
ta --db analysis.sqlite theme-code --codebook-version 30

# Retry failed runs
ta --db analysis.sqlite theme-code --retry-failed
```

### 3. Aggregate themes

Runs once all theme coders have finished. Merges similar themes across
coders into a final consolidated set.

```bash
ta --db analysis.sqlite theme-aggregate

# Retry a failed aggregation
ta --db analysis.sqlite theme-aggregate --retry-failed

# Aggregate against a specific codebook version
ta --db analysis.sqlite theme-aggregate --codebook-version 30
```

### 4. Check progress

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

```
codebook_versions   — append-only codebook snapshots (JSON blobs)
coders              — registered Stage 1 coder identities
segments            — input text segments (pending → coding → aggregating → reviewing → done)
coder_runs          — one row per (segment, coder); status running|done|failed
coder_codes         — individual codes produced by each coder run
aggregations        — one row per segment after all coders finish
aggregated_codes    — merged codes produced by the aggregator
review_decisions    — reviewer's decision per aggregated code

theme_coders        — registered Stage 2 theme coder identities
theme_coder_runs    — one row per (theme_coder, codebook_version); status running|done|failed
theme_aggregations  — one row per codebook_version after all theme coders finish
theme_aggregation_inputs — links each aggregation to the runs that fed it
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
ta --db DB aggregate       [--limit N] [--retry-failed] [--mock-embeddings]
ta --db DB review          [--limit N] [--mock-embeddings]
ta --db DB status
ta --db DB export-codebook [--version N] [-o FILE]

ta --db DB add-theme-coder   ID IDENTITY
ta --db DB rm-theme-coder    ID [--force]
ta --db DB list-theme-coders
ta --db DB theme-code        [--codebook-version N] [--workers K] [--limit N]
                                    [--retry-failed] [--mock-embeddings]
ta --db DB theme-aggregate   [--codebook-version N] [--retry-failed]
                                    [--mock-embeddings]
ta --db DB theme-status      [--codebook-version N]
ta --db DB export-themes        [--codebook-version N] [-o FILE]
ta --db DB export-themes-html   [--codebook-version N] [-o FILE]
```
