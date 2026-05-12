# Thematic Analysis

An LLM-powered tool for automated thematic analysis of qualitative data. Analyze PDFs, text files, or raw text to discover themes and patterns.

Built with the [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk).

## Installation

```bash
pip install thematic-analysis

# Or from source
git clone https://github.com/neubig/thematic-analysis.git
cd thematic-analysis
pip install -e .
```

## Quick Start

### Analyze a Directory of PDFs

```python
from thematic_analysis import ThematicAnalysisPipeline

pipeline = ThematicAnalysisPipeline()
result = pipeline.run_from_directory("/path/to/pdfs")

# View discovered themes
for theme in result.themes.themes:
    print(f"{theme.name}: {theme.description}")
    print(f"  Codes: {', '.join(theme.original_themes)}")
```

### Analyze Text Directly

```python
from thematic_analysis import ThematicAnalysisPipeline

texts = [
    "I feel overwhelmed by the amount of work...",
    "The team collaboration has been excellent...",
    "Deadlines are causing significant stress...",
]

pipeline = ThematicAnalysisPipeline()
result = pipeline.run_from_texts(texts)
```

### Analyze a Single PDF

```python
from thematic_analysis import ThematicAnalysisPipeline

pipeline = ThematicAnalysisPipeline()
result = pipeline.run_from_pdf("/path/to/document.pdf")
```

## Configuration

Customize the analysis with `PipelineConfig`:

```python
from thematic_analysis import ThematicAnalysisPipeline, PipelineConfig

config = PipelineConfig(
    # Number of parallel coders (more = diverse perspectives)
    num_coders=3,
    
    # Number of theme developers  
    num_theme_coders=2,
    
    # LLM model to use
    model="anthropic/claude-sonnet-4-20250514",
    
    # Processing batch size
    batch_size=10,
)

pipeline = ThematicAnalysisPipeline(config=config)
```

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `num_coders` | 3 | Number of independent coders. More coders = more diverse code suggestions |
| `num_theme_coders` | 2 | Number of theme developers |
| `batch_size` | 10 | Segments processed per batch |
| `execution_mode` | `PARALLEL` | `PARALLEL` for speed, `SEQUENTIAL` for debugging |

### Coder Configuration

```python
from thematic_analysis.agents import CoderConfig

coder_config = CoderConfig(
    model="anthropic/claude-sonnet-4-20250514",
    max_codes_per_segment=5,  # Max codes assigned to each text segment
    temperature=0.7,          # LLM temperature (higher = more creative)
)

config = PipelineConfig(coder_config=coder_config)
```

### Using Identity Perspectives

Assign different analytical perspectives to coders for richer analysis:

```python
from thematic_analysis import PipelineConfig

config = PipelineConfig(
    num_coders=3,
    coder_identities=[
        "a healthcare professional focused on patient outcomes",
        "an economist analyzing cost-effectiveness",
        "a patient advocate prioritizing accessibility",
    ],
)
```

## Output Format

The pipeline returns a `PipelineResult` with:

```python
result = pipeline.run_from_texts(texts)

# Themes discovered
result.themes.themes  # List of MergedTheme objects

# Each theme has:
# - name: str
# - description: str  
# - original_themes: list[str] (the codes that were merged into this theme)

# The codebook with all codes
result.codebook

# Export to JSON
result.to_json()
```

## Environment Variables

Set your LLM API credentials:

```bash
export LLM_API_KEY="your-api-key"
export LLM_MODEL="claude-sonnet-4-6"  # or other supported model
```

## Supported File Formats

- **PDF** (`.pdf`) - Automatic text extraction
- **Text** (`.txt`) - Plain text files
- **Markdown** (`.md`) - Markdown files

## How It Works

The pipeline uses a multi-agent architecture:

1. **Coders**: Multiple independent agents analyze text segments and assign codes
2. **Aggregator**: Merges similar codes from different coders
3. **Reviewer**: Validates and refines the codebook
4. **Theme Coders**: Group codes into higher-level themes
5. **Theme Aggregator**: Produces final consolidated themes

---

## Incremental Pipeline (`ta-stage1` / `ta-stage2`)

The incremental pipeline is a SQLite-backed alternative that stores every
intermediate result in a database, so each step can be run separately,
resumed after failure, and rerun without redoing finished work.

### Setup

```bash
# Point at the same SQLite file for every command
export DB=analysis.sqlite

# Initialise the database (creates schema + empty codebook v1)
ta-stage1 --db $DB init
```

### Environment Variables

The agents call an LLM via the OpenHands SDK. Set at minimum:

```bash
export LLM_MODEL=anthropic/claude-sonnet-4-6   # or any supported model
export LLM_API_KEY=your-api-key
```

---

### Stage 1 — Coding, Aggregation, Review

**1. Load your data**

```bash
# From .txt / .md files (auto-segmented by paragraph)
ta-stage1 --db $DB add-document interviews/*.txt

# Or from a JSON/JSONL file with {segment_id, text} records
ta-stage1 --db $DB enqueue --segments segments.jsonl
```

**2. Register coders** (each gets an independent analytical perspective)

```bash
ta-stage1 --db $DB add-coder alice "feminist scholar"
ta-stage1 --db $DB add-coder bob   "grounded theory researcher"
ta-stage1 --db $DB list-coders
```

**3. Run coding** (can be run in parallel across machines sharing the DB)

```bash
ta-stage1 --db $DB code alice --workers 4
ta-stage1 --db $DB code bob   --workers 4

# Retry any failed segments
ta-stage1 --db $DB code alice --retry-failed
```

**4. Aggregate codes** (merges codes from all coders per segment)

```bash
ta-stage1 --db $DB aggregate
```

**5. Review and build the codebook**

```bash
ta-stage1 --db $DB review
```

**6. Check progress at any point**

```bash
ta-stage1 --db $DB status
# segments:         120 total | done=118 reviewing=2
# coders:           2
# coder_runs:       240 total | done=238 failed=2
# aggregations:     120 total | done=120
# review_decisions: 487 total | applied=487
# codebook:         v=52 codes=41

# Export the final codebook
ta-stage1 --db $DB export-codebook -o codebook.json
```

---

### Stage 2 — Theme Development

Stage 2 reads the codebook produced by Stage 1 from the same DB and
develops higher-level themes. Run it after Stage 1 `review` is complete.

**1. Register theme coders**

```bash
ta-stage2 --db $DB add-theme-coder t1 "critical discourse analyst"
ta-stage2 --db $DB add-theme-coder t2 "phenomenological researcher"
ta-stage2 --db $DB list-theme-coders
```

**2. Develop themes** (each coder works independently against the codebook)

```bash
# Uses the latest codebook version by default; pin with --codebook-version N
ta-stage2 --db $DB theme-code --workers 2

# Retry failed coders
ta-stage2 --db $DB theme-code --retry-failed
```

**3. Aggregate themes** (runs once all theme coders are done)

```bash
ta-stage2 --db $DB theme-aggregate
```

**4. Check progress and export**

```bash
ta-stage2 --db $DB status
# codebook version:     v52
# theme_coders:         2
# theme_coder_runs:     2 total | done=2
# theme_aggregations:   1 total | done=1
# themes in result:     5

ta-stage2 --db $DB export-themes -o themes.json
```

---

### Running against a specific codebook version

Both `theme-code` and `theme-aggregate` accept `--codebook-version N` to
pin the analysis to an earlier snapshot, which is useful for reproducing
results or experimenting with a different codebook state:

```bash
ta-stage2 --db $DB theme-code      --codebook-version 30
ta-stage2 --db $DB theme-aggregate --codebook-version 30
ta-stage2 --db $DB export-themes   --codebook-version 30 -o themes_v30.json
```

---

### Full command reference

```
ta-stage1 --db DB init
ta-stage1 --db DB add-coder     ID IDENTITY
ta-stage1 --db DB rm-coder      ID [--force]
ta-stage1 --db DB list-coders
ta-stage1 --db DB add-document  FILES... [--segmentation paragraph|sentence|fixed]
ta-stage1 --db DB enqueue       --segments FILE [--batch N]
ta-stage1 --db DB code          ID [--workers K] [--limit N] [--retry-failed] [--mock-embeddings]
ta-stage1 --db DB aggregate     [--limit N] [--retry-failed]
ta-stage1 --db DB review        [--limit N]
ta-stage1 --db DB status
ta-stage1 --db DB export-codebook [--version N] [-o FILE]

ta-stage2 --db DB init
ta-stage2 --db DB add-theme-coder     ID IDENTITY
ta-stage2 --db DB rm-theme-coder      ID [--force]
ta-stage2 --db DB list-theme-coders
ta-stage2 --db DB theme-code          [--codebook-version N] [--workers K] [--limit N] [--retry-failed]
ta-stage2 --db DB theme-aggregate     [--codebook-version N] [--retry-failed]
ta-stage2 --db DB status              [--codebook-version N]
ta-stage2 --db DB export-themes       [--codebook-version N] [-o FILE]
```

## License

MIT License - See [LICENSE](LICENSE) for details.

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
