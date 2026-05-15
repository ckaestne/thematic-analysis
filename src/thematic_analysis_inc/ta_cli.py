"""`ta` — command-style CLI for thematic-analysis utilities.

Each subcommand is a small function registered in COMMANDS. New commands
should add one entry there and one `_cmd_<name>` function.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from thematic_analysis.loaders import load_document


def _cmd_segment(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    doc = load_document(path)

    titles: list[str] | None = None
    if args.method == "llm":
        from thematic_analysis_inc.segmenter_llm import segment_by_llm

        doc_id = path.stem
        segments, boundaries = segment_by_llm(
            doc.text,
            doc_id=doc_id,
            model=args.model,
            min_words=args.min_words,
        )
        # Boundaries may outnumber final segments after merging; align titles
        # to surviving segment_ids by start_line.
        by_line = {f"{doc_id}_l{b.start_line}": b.title for b in boundaries}
        titles = [by_line.get(seg.segment_id, "") for seg in segments]
    else:
        segments = doc.segment(
            method=args.method,
            min_words=args.min_words,
            max_words=args.max_words,
        )

    if not segments:
        print("(no segments produced)", file=sys.stderr)
        return 1

    for i, seg in enumerate(segments, 1):
        words = len(seg.text.split())
        title = f"  — {titles[i - 1]}" if titles and titles[i - 1] else ""
        print(f"── [{i}/{len(segments)}] {seg.segment_id}  ({words} words){title}")
        print(seg.text)
        print()

    print(f"── {len(segments)} segment(s) from {path.name}", file=sys.stderr)
    return 0


def _add_segment_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("segment", help="segment a document and print segments")
    p.add_argument("file", help="path to .md/.txt/.pdf")
    p.add_argument(
        "--method",
        choices=("paragraph", "sentence", "fixed", "llm"),
        default="paragraph",
    )
    p.add_argument(
        "--min-words",
        type=int,
        default=20,
        help="minimum words per segment (for paragraph/sentence: drop; for llm: merge)",
    )
    p.add_argument("--max-words", type=int, default=500)
    p.add_argument(
        "--model",
        default="gemini/gemini-2.5-flash-lite",
        help="litellm model id for --method llm (e.g. gemini/gemini-2.5-pro)",
    )
    p.set_defaults(func=_cmd_segment)


COMMANDS: list[Callable[[argparse._SubParsersAction], None]] = [
    _add_segment_parser,
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ta", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for register in COMMANDS:
        register(sub)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
