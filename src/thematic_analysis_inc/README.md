# Incremental Thematic Analysis Pipeline

A SQLite-backed, step-by-step reimplementation of the thematic analysis
pipeline. Every intermediate result is stored in the database, so each step
can be run separately, resumed after failure, and rerun without redoing
finished work.

Two CLI entry points, one for each stage:

| Command | Stage |
|---|---|
| `ta-stage1` | Coding → Aggregation → Review → Codebook |
| `ta-stage2` | Theme coding → Theme aggregation → Final themes |

Both commands share the same SQLite file.

---

## Installation

```bash
git clone https://github.com/ckaestne/thematic-analysis.git
cd thematic-analysis
pip install -e .
```

`pip install -e .` registers the `ta-stage1` and `ta-stage2` console
scripts into your active Python environment. After that you can call them
directly:

```bash
ta-stage1 --help
ta-stage2 --help
```

If the commands are not found, check that the environment's `bin/` (or
`Scripts/` on Windows) is on your `PATH`. With a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
ta-stage1 --help
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
ta-stage1 --db analysis.sqlite init
```

Creates the schema and an empty codebook (version 1). Safe to re-run on an
existing database.

### 2. Load data

```bash
# From .txt or .md files — auto-segmented by paragraph
ta-stage1 --db analysis.sqlite add-document interviews/*.txt

# From a JSON array or JSONL file with {segment_id, text} objects
ta-stage1 --db analysis.sqlite enqueue --segments segments.jsonl

# Optional: group segments into numbered batches
ta-stage1 --db analysis.sqlite enqueue --segments segments.jsonl --batch 1
```

`enqueue` and `add-document` are idempotent: re-running them skips segments
whose `segment_id` already exists.

### 3. Register coders

Each coder gets an independent analytical perspective shown to the agent.

```bash
ta-stage1 --db analysis.sqlite add-coder alice "feminist scholar"
ta-stage1 --db analysis.sqlite add-coder bob   "grounded theory researcher"
ta-stage1 --db analysis.sqlite list-coders
```

### 4. Code segments

Each coder works through all segments independently. Multiple coders can
run in parallel (even on different machines sharing the same DB file).

```bash
ta-stage1 --db analysis.sqlite code alice --workers 4
ta-stage1 --db analysis.sqlite code bob   --workers 4

# Retry any failed or stalled runs before starting
ta-stage1 --db analysis.sqlite code alice --retry-failed

# Process only the next N segments
ta-stage1 --db analysis.sqlite code alice --limit 10
```

### 5. Aggregate codes

Merges codes from all coders for each segment. Runs serially; safe to
restart — already-aggregated segments are skipped.

```bash
ta-stage1 --db analysis.sqlite aggregate

# Retry failed aggregations
ta-stage1 --db analysis.sqlite aggregate --retry-failed
```

### 6. Review and build the codebook

The reviewer processes each aggregated code one at a time, deciding whether
to add it to the codebook, merge it with an existing code, update an
existing code, or skip it. Each decision that changes the codebook creates
a new versioned snapshot.

```bash
ta-stage1 --db analysis.sqlite review

# Process only the next N codes
ta-stage1 --db analysis.sqlite review --limit 20
```

### 7. Check progress

```bash
ta-stage1 --db analysis.sqlite status
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
ta-stage1 --db analysis.sqlite export-codebook -o codebook.json

# A specific earlier version
ta-stage1 --db analysis.sqlite export-codebook --version 30 -o codebook_v30.json
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
ta-stage2 --db analysis.sqlite set-research-context \
    --aim "Understand how lay users justify climate-policy skepticism" \
    --research-question "What rhetorical strategies do skeptics use to justify inaction?" \
    --domain "climate change" \
    --theoretical-framework "critical discourse analysis"

ta-stage2 --db analysis.sqlite show-research-context
```

You can also pass `--file context.json` with the same field names.

### 1. Register theme coders

```bash
ta-stage2 --db analysis.sqlite add-theme-coder t1 "critical discourse analyst"
ta-stage2 --db analysis.sqlite add-theme-coder t2 "phenomenological researcher"
ta-stage2 --db analysis.sqlite list-theme-coders
```

### 2. Develop themes

Each theme coder independently reads the codebook and proposes a set of
themes. Multiple coders can run in parallel.

```bash
# Uses the latest codebook version by default
ta-stage2 --db analysis.sqlite theme-code --workers 2

# Pin to a specific codebook version
ta-stage2 --db analysis.sqlite theme-code --codebook-version 30

# Retry failed runs
ta-stage2 --db analysis.sqlite theme-code --retry-failed
```

### 3. Aggregate themes

Runs once all theme coders have finished. Merges similar themes across
coders into a final consolidated set.

```bash
ta-stage2 --db analysis.sqlite theme-aggregate

# Retry a failed aggregation
ta-stage2 --db analysis.sqlite theme-aggregate --retry-failed

# Aggregate against a specific codebook version
ta-stage2 --db analysis.sqlite theme-aggregate --codebook-version 30
```

### 4. Check progress

```bash
ta-stage2 --db analysis.sqlite status
# codebook version:     v52
# theme_coders:         2
# theme_coder_runs:     2 total | done=2
# theme_aggregations:   1 total | done=1
# themes in result:     5
```

### 5. Export themes

```bash
ta-stage2 --db analysis.sqlite export-themes -o themes.json

# From a specific codebook version
ta-stage2 --db analysis.sqlite export-themes --codebook-version 30 -o themes_v30.json
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
ta-stage1 --db DB init
ta-stage1 --db DB add-coder       ID IDENTITY
ta-stage1 --db DB rm-coder        ID [--force]
ta-stage1 --db DB list-coders
ta-stage1 --db DB add-document    FILES... [--segmentation paragraph|sentence|fixed]
                                           [--min-words N] [--max-words N] [--batch N]
ta-stage1 --db DB enqueue         --segments FILE [--batch N]
ta-stage1 --db DB code            ID [--workers K] [--limit N] [--retry-failed]
                                     [--mock-embeddings]
ta-stage1 --db DB aggregate       [--limit N] [--retry-failed] [--mock-embeddings]
ta-stage1 --db DB review          [--limit N] [--mock-embeddings]
ta-stage1 --db DB status
ta-stage1 --db DB export-codebook [--version N] [-o FILE]

ta-stage2 --db DB init
ta-stage2 --db DB add-theme-coder   ID IDENTITY
ta-stage2 --db DB rm-theme-coder    ID [--force]
ta-stage2 --db DB list-theme-coders
ta-stage2 --db DB theme-code        [--codebook-version N] [--workers K] [--limit N]
                                    [--retry-failed] [--mock-embeddings]
ta-stage2 --db DB theme-aggregate   [--codebook-version N] [--retry-failed]
                                    [--mock-embeddings]
ta-stage2 --db DB status            [--codebook-version N]
ta-stage2 --db DB export-themes     [--codebook-version N] [-o FILE]
```
