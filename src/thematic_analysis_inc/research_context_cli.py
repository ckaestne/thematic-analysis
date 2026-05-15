"""CLI helpers for managing the stored research context.

Shared between the Stage 1 (`ta-stage1`) and Stage 2 (`ta-stage2`) entry
points so the same subcommands behave identically. The research context is
a singleton row keyed `id = 1` in the `research_context` table.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from thematic_analysis.research_context import AGENT_ROLES, ResearchContext
from thematic_analysis_inc import store


def _load_description(args: argparse.Namespace) -> str:
    if args.description_file:
        return Path(args.description_file).read_text(encoding="utf-8")
    if args.description is not None:
        return args.description
    return ""


def cmd_set(args: argparse.Namespace) -> int:
    description = _load_description(args).strip()
    if not description:
        print(
            "refusing to set an empty research context; "
            "provide --description or --description-file",
            file=sys.stderr,
        )
        return 1

    conn = store.connect(args.db)
    existing = store.get_research_context(conn)
    # Preserve tailored prompts only if description is unchanged.
    if existing is not None and existing.description == description:
        tailored = dict(existing.tailored_prompts)
    else:
        tailored = {}
    ctx = ResearchContext(description=description, tailored_prompts=tailored)
    store.set_research_context(conn, ctx)
    print("research context saved")

    if args.regenerate_prompts:
        from thematic_analysis.research_context_tailor import (
            generate_all_tailored_prompts,
        )

        print(f"generating tailored prompts for: {', '.join(AGENT_ROLES)} ...")
        prompts = generate_all_tailored_prompts(description)
        store.set_research_context(
            conn,
            ResearchContext(description=description, tailored_prompts=prompts),
        )
        print(f"tailored prompts saved ({len(prompts)} roles)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    ctx = store.get_research_context(conn)
    if ctx is None:
        print("(no research context set)")
        return 0
    print("# Description")
    print(ctx.description.strip() or "(empty)")
    if ctx.tailored_prompts:
        for role in AGENT_ROLES:
            section = ctx.tailored_prompts.get(role)
            if not section:
                continue
            print()
            print(f"# Tailored prompt — {role}")
            print(section.strip())
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
    p_set.add_argument(
        "--description",
        default=None,
        help="freeform research context + research question(s) as a single string",
    )
    p_set.add_argument(
        "--description-file",
        default=None,
        help="path to a text file containing the research context description",
    )
    p_set.add_argument(
        "--regenerate-prompts",
        action="store_true",
        help="after saving, call the LLM to generate per-agent tailored prompts",
    )
    p_set.set_defaults(func=cmd_set)

    p_show = sub.add_parser(
        "show-research-context",
        help="print the stored research context",
    )
    p_show.set_defaults(func=cmd_show)

    p_clear = sub.add_parser(
        "clear-research-context",
        help="delete the stored research context",
    )
    p_clear.set_defaults(func=cmd_clear)
