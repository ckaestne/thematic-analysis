"""CLI helpers for managing the stored research context.

Shared between the Stage 1 (`ta-stage1`) and Stage 2 (`ta-stage2`) entry
points so the same subcommands behave identically. The research context is
a singleton row keyed `id = 1` in the `research_context` table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from thematic_analysis.research_context import ResearchContext
from thematic_analysis_inc import store


def _load_from_file(path: Path) -> ResearchContext:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("research-context file must contain a JSON object")
    return ResearchContext(
        title=data.get("title", ""),
        aim=data.get("aim", ""),
        research_questions=list(data.get("research_questions", [])),
        theoretical_framework=data.get("theoretical_framework", ""),
        paradigm=data.get("paradigm", ""),
        methodology=data.get("methodology", "thematic_analysis"),
        domain=data.get("domain", ""),
        background=data.get("background", ""),
        keywords=list(data.get("keywords", [])),
    )


def _ctx_from_args(args: argparse.Namespace) -> ResearchContext:
    if args.file:
        return _load_from_file(Path(args.file))
    rqs = list(args.research_question or [])
    kws = list(args.keyword or [])
    ctx = ResearchContext(
        title=args.title or "",
        aim=args.aim or "",
        research_questions=rqs,
        theoretical_framework=args.theoretical_framework or "",
        paradigm=args.paradigm or "",
        methodology=args.methodology or "thematic_analysis",
        domain=args.domain or "",
        background=args.background or "",
        keywords=kws,
    )
    return ctx


def cmd_set(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    ctx = _ctx_from_args(args)
    if ctx.is_empty():
        print(
            "refusing to set an empty research context; "
            "provide --aim/--research-question/... or --file",
            file=sys.stderr,
        )
        return 1
    store.set_research_context(conn, ctx)
    print("research context saved")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    ctx = store.get_research_context(conn)
    if ctx is None:
        print("(no research context set)")
        return 0
    section = ctx.to_prompt_section()
    print(section if section else "(empty research context)")
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    removed = store.clear_research_context(conn)
    print("research context cleared" if removed else "no research context to clear")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    """Attach set/show/clear-research-context subcommands to a CLI."""
    p_set = sub.add_parser(
        "set-research-context",
        help="store the research context used by Stage 1 + Stage 2 prompts",
    )
    p_set.add_argument("--title", default=None)
    p_set.add_argument("--aim", default=None, help="primary research aim")
    p_set.add_argument(
        "--research-question",
        action="append",
        default=None,
        help="research question (repeatable)",
    )
    p_set.add_argument("--theoretical-framework", default=None)
    p_set.add_argument("--paradigm", default=None)
    p_set.add_argument("--methodology", default=None)
    p_set.add_argument("--domain", default=None)
    p_set.add_argument("--background", default=None)
    p_set.add_argument(
        "--keyword",
        action="append",
        default=None,
        help="key concept (repeatable)",
    )
    p_set.add_argument(
        "--file",
        default=None,
        help="path to a JSON file with the ResearchContext fields (overrides flags)",
    )
    p_set.set_defaults(func=cmd_set)

    p_show = sub.add_parser(
        "show-research-context",
        help="print the stored research context as it appears in prompts",
    )
    p_show.set_defaults(func=cmd_show)

    p_clear = sub.add_parser(
        "clear-research-context",
        help="delete the stored research context",
    )
    p_clear.set_defaults(func=cmd_clear)
