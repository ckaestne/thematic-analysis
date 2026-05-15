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

    if args.method == "llm":
        from thematic_analysis_inc.segmenter_llm import segment_by_llm

        titled = segment_by_llm(
            doc.text,
            doc_id=path.stem,
            model=args.model,
            min_words=args.min_words,
        )
        rows: list[tuple[str, str, str]] = [
            (s.segment_id, s.text, s.title) for s in titled
        ]
    else:
        segments = doc.segment(
            method=args.method,
            min_words=args.min_words,
            max_words=args.max_words,
        )
        rows = [(s.segment_id, s.text, "") for s in segments]

    if not rows:
        print("(no segments produced)", file=sys.stderr)
        return 1

    for i, (seg_id, text, title) in enumerate(rows, 1):
        words = len(text.split())
        suffix = f"  — {title}" if title else ""
        print(f"── [{i}/{len(rows)}] {seg_id}  ({words} words){suffix}")
        print(text)
        print()

    print(f"── {len(rows)} segment(s) from {path.name}", file=sys.stderr)
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
