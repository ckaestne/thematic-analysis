#!/usr/bin/env python3
"""Run thematic analysis on a directory of PDFs."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Get model from environment or use default
MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")


def main():
    """Run the thematic analysis pipeline on PDFs."""
    from thematic_analysis import PipelineConfig, ThematicLMPipeline
    from thematic_analysis.agents import CoderConfig, AggregatorConfig, ThemeCoderConfig
    from thematic_analysis.pipeline import ExecutionMode

    pdf_dir = sys.argv[1] if len(sys.argv) > 1 else "/workspace/project/paper1-pdfs"
    pattern = sys.argv[2] if len(sys.argv) > 2 else "*.pdf"

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
    print()

    # Configure pipeline with real LLM
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
        batch_size=5,  # Process 5 segments at a time
        use_mock_embeddings=False,  # Use real embeddings
        execution_mode=ExecutionMode.SEQUENTIAL,  # Use sequential for stability
        debug_dir=debug_dir,
    )

    print(f"Debug dumps: {debug_dir}")

    pipeline = ThematicLMPipeline(config=config)

    print("Running thematic analysis...")
    print("-" * 60)
    
    result = pipeline.run_from_directory(
        pdf_dir,
        pattern=pattern,
        segmentation="paragraph",
        min_words=30,  # Skip very short paragraphs
    )

    print()
    print("=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)
    print()
    
    # Print themes
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

    # Save results
    output_path = Path("analysis_results.json")
    output_path.write_text(result.to_json())
    print(f"\nResults saved to: {output_path}")

    return result


if __name__ == "__main__":
    main()
