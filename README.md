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

A SQLite-backed alternative that stores every intermediate result so each
step can be run separately, resumed after failure, and rerun without
redoing finished work. See
[`src/thematic_analysis_inc/README.md`](src/thematic_analysis_inc/README.md)
for the full usage guide.

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
