"""CLI helpers for managing the stored research context.

Registered under the unified `ta` CLI. The research context is versioned:
each ``set`` creates a new row; ``show`` reads the latest; ``clear``
wipes history.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import typer

from thematic_analysis.research_context import AGENT_ROLES, ResearchContext
from thematic_analysis_inc import db as store  # noqa: N812 — keep `store` name local


def _load_description(args: SimpleNamespace) -> str:
    if args.description_file:
        return Path(args.description_file).read_text(encoding="utf-8")
    if args.description is not None:
        return args.description
    return ""


def cmd_set(args: SimpleNamespace) -> int:
    description = _load_description(args).strip()
    if not description:
        print(
            "refusing to set an empty research context; "
            "provide --description or --description-file",
            file=sys.stderr,
        )
        return 1

    store.connect(args.db)
    existing = store.get_research_context()
    # Preserve tailored prompts only if description is unchanged.
    if existing is not None and existing.description == description:
        tailored = dict(store.research_context_to_domain(existing).tailored_prompts)
    else:
        tailored = {}
    ctx = ResearchContext(description=description, tailored_prompts=tailored)
    rc = store.add_research_context_and_codebook_revision(ctx)
    print(f"research context saved (version {rc.research_context_version})")

    if args.regenerate_prompts:
        from thematic_analysis.research_context_tailor import (
            generate_all_tailored_prompts,
        )

        print(f"generating tailored prompts for: {', '.join(AGENT_ROLES)} ...")
        prompts = generate_all_tailored_prompts(description)
        rc = store.add_research_context_and_codebook_revision(
            ResearchContext(description=description, tailored_prompts=prompts),
        )
        print(
            f"tailored prompts saved ({len(prompts)} roles) "
            f"as version {rc.research_context_version}"
        )
    return 0


def cmd_show(args: SimpleNamespace) -> int:
    store.connect(args.db)
    rc = store.get_research_context()
    if rc is None:
        print("(no research context set)")
        return 0
    ctx = store.research_context_to_domain(rc)
    print(f"# Description (version {rc.research_context_version})")
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


def cmd_clear(args: SimpleNamespace) -> int:
    store.connect(args.db)
    removed = store.clear_research_context()
    print("research context cleared" if removed else "no research context to clear")
    return 0


def register_typer(app: typer.Typer, run, *, panel: str) -> None:
    """Attach set/show/clear-research-context subcommands to a Typer app."""

    @app.command(
        name="set-research-context",
        rich_help_panel=panel,
        help="store the research context used by Stage 1 prompts",
    )
    def _set(
        ctx: typer.Context,
        description: Annotated[
            str | None,
            typer.Option(
                "--description",
                help=(
                    "freeform research context + research question(s) "
                    "as a single string"
                ),
            ),
        ] = None,
        description_file: Annotated[
            str | None,
            typer.Option(
                "--description-file",
                help="path to a text file containing the research context description",
            ),
        ] = None,
        regenerate_prompts: Annotated[
            bool,
            typer.Option(
                "--regenerate-prompts",
                help=(
                    "after saving, call the LLM to generate per-agent "
                    "tailored prompts"
                ),
            ),
        ] = False,
    ) -> None:
        run(
            ctx,
            cmd_set,
            description=description,
            description_file=description_file,
            regenerate_prompts=regenerate_prompts,
        )

    @app.command(
        name="show-research-context",
        rich_help_panel=panel,
        help="print the stored research context",
    )
    def _show(ctx: typer.Context) -> None:
        run(ctx, cmd_show)

    @app.command(
        name="clear-research-context",
        rich_help_panel=panel,
        help="delete the stored research context",
    )
    def _clear(ctx: typer.Context) -> None:
        run(ctx, cmd_clear)
