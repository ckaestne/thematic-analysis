#!/usr/bin/env python3
"""Run thematic analysis on a directory of PDFs."""

import argparse
import os
from datetime import datetime
from pathlib import Path

MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")


def _load_research_context(path: str):
    from thematic_analysis.research_context import ResearchContext

    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Research question file is empty: {path}")
    return ResearchContext(
        title="Research Focus",
        aim=text,
        background=text,
    )


def main():
    """Run the thematic analysis pipeline on PDFs."""
    from thematic_analysis import PipelineConfig, ThematicLMPipeline
    from thematic_analysis.agents import (
        AggregatorConfig,
        CoderConfig,
        ThemeCoderConfig,
    )
    from thematic_analysis.pipeline import ExecutionMode

    parser = argparse.ArgumentParser(description="Run thematic analysis on PDFs.")
    parser.add_argument(
        "pdf_dir",
        nargs="?",
        default="/workspace/project/paper1-pdfs",
        help="Directory of PDFs to analyse.",
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        default="*.pdf",
        help="Glob pattern for files (default: *.pdf).",
    )
    parser.add_argument(
        "--researchquestion",
        type=str,
        default=None,
        help=(
            "Path to a text file describing the research question / focus. "
            "Used to keep coding and theme development on-topic."
        ),
    )
    args = parser.parse_args()

    pdf_dir = args.pdf_dir
    pattern = args.pattern

    research_context = None
    if args.researchquestion:
        research_context = _load_research_context(args.researchquestion)

    debug_dir = os.environ.get(
        "THEMATIC_DEBUG_DIR",
        f"debug/{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )

    print("=" * 60)
    print("Thematic-LM Analysis")
    print("=" * 60)
    print(f"Directory: {pdf_dir}")
    print(f"Pattern: {pattern}")
    print(f"Model: {MODEL}")
    if research_context:
        print(f"Research focus: {args.researchquestion}")
        preview = research_context.aim.replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "..."
        print(f"  > {preview}")
    print()

    config = PipelineConfig(
        num_coders=3,
        num_theme_coders=2,
        coder_config=CoderConfig(
            model=MODEL,
            max_codes_per_segment=5,
            temperature=0.7,
        ),
        aggregator_config=AggregatorConfig(
            model=MODEL,
            similarity_threshold=0.75,
        ),
        theme_coder_config=ThemeCoderConfig(
            model=MODEL,
            max_themes=10,
            min_codes_per_theme=2,
        ),
        batch_size=5,
        use_mock_embeddings=False,
        execution_mode=ExecutionMode.SEQUENTIAL,
        debug_dir=debug_dir,
        research_context=research_context,
    )

    print(f"Debug dumps: {debug_dir}")

    pipeline = ThematicLMPipeline(config=config)

    print("Running thematic analysis...")
    print("-" * 60)

    result = pipeline.run_from_directory(
        pdf_dir,
        pattern=pattern,
        segmentation="paragraph",
        min_words=30,
    )

    print()
    print("=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)
    print()

    print("THEMES DISCOVERED:")
    print("-" * 40)
    for i, theme in enumerate(result.themes.themes, 1):
        print(f"\n{i}. {theme.name}")
        print(f"   Description: {theme.description}")
        print(f"   Codes: {', '.join(theme.codes[:5])}{'...' if len(theme.codes) > 5 else ''}")
        if theme.quotes:
            print(f"   Example quote: \"{theme.quotes[0].text[:100]}...\"")

    print()
    print("-" * 40)
    print(f"Total themes: {len(result.themes.themes)}")
    print(f"Total codes in codebook: {len(result.codebook)}")
    print(f"Segments processed: {result.metrics.get('num_segments', 'N/A')}")

    output_path = Path("analysis_results.json")
    output_path.write_text(result.to_json())
    print(f"\nResults saved to: {output_path}")

    return result


if __name__ == "__main__":
    main()
