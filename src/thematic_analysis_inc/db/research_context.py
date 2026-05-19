"""Versioned research-context CRUD, backed by SQLModel.

The research context is no longer a singleton: each call to
:func:`set_research_context` inserts a new row keyed by an
autoincrementing ``research_context_version``. The latest row is the
"current" research context. Other tables that depend on the context
active when a row was produced (``codes``, ``coding_queue``,
``theme_coder_runs``, ``theme_aggregations``) carry a nullable FK back
to a specific revision.

This module is the first part of the SQLModel migration: it uses the
:class:`~thematic_analysis_inc.db.models.ResearchContext` model
directly. Callers receive SQLModel objects and use ``rc.description``,
``rc.research_context_version`` etc. as attributes. For places that
still consume the domain dataclass (the LLM agents), use
:func:`to_domain` to adapt.
"""

from __future__ import annotations

from sqlmodel import select

from thematic_analysis.research_context import AGENT_ROLES
from thematic_analysis.research_context import (
    ResearchContext as DomainResearchContext,
)

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import ResearchContext


# Re-exported so other ``db/`` modules don't reach into the
# ``thematic_analysis`` package.
RC_AGENT_ROLES: tuple[str, ...] = AGENT_ROLES


# Role name → SQLModel column attribute on ``ResearchContext``.
_ROLE_TO_ATTR: dict[str, str] = {
    "coder": "coder_prompt",
    "coding_critic": "coding_critic_prompt",
    "reviewer": "reviewer_prompt",
    "theme_coder": "theme_coder_prompt",
    "theme_aggregator": "theme_aggregator_prompt",
}


# ---------------------------------------------------------------------------
# Conversion to / from the agents' domain dataclass
# ---------------------------------------------------------------------------


def to_domain(rc: ResearchContext) -> DomainResearchContext:
    """Adapt a SQLModel row to the dataclass the LLM agents consume."""
    tailored: dict[str, str] = {}
    for role, attr in _ROLE_TO_ATTR.items():
        val = getattr(rc, attr)
        if val:
            tailored[role] = val
    return DomainResearchContext(
        description=rc.description or "",
        tailored_prompts=tailored,
    )


def _from_domain(ctx: DomainResearchContext) -> ResearchContext:
    """Build a fresh SQLModel row from a domain dataclass."""
    return ResearchContext(
        description=ctx.description or "",
        coder_prompt=ctx.tailored_prompts.get("coder") or None,
        coding_critic_prompt=ctx.tailored_prompts.get("coding_critic") or None,
        reviewer_prompt=ctx.tailored_prompts.get("reviewer") or None,
        theme_coder_prompt=ctx.tailored_prompts.get("theme_coder") or None,
        theme_aggregator_prompt=(
            ctx.tailored_prompts.get("theme_aggregator") or None
        ),
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def set_research_context(ctx: DomainResearchContext) -> ResearchContext:
    """Insert a new research-context revision. Returns the persisted row.

    History is preserved: previous revisions stay in the table so codes
    / queue rows / theme runs that reference them remain valid.
    """
    rc = _from_domain(ctx)
    with session() as s:
        s.add(rc)
        s.commit()
        s.refresh(rc)
        # Detach so callers can use attributes after the session closes.
        s.expunge(rc)
        return rc


def get_research_context(
    version: int | None = None,
) -> ResearchContext | None:
    """Return the latest revision (default) or a specific one.

    Returns ``None`` if the table is empty or the requested version
    doesn't exist.
    """
    with session() as s:
        if version is None:
            rc = s.exec(
                select(ResearchContext)
                .order_by(
                    ResearchContext.research_context_version.desc()  # type: ignore[union-attr]
                )
                .limit(1)
            ).first()
        else:
            rc = s.get(ResearchContext, version)
        if rc is not None:
            s.expunge(rc)
        return rc


def latest_research_context_version() -> int | None:
    """Latest version id, or ``None`` if no revisions exist."""
    rc = get_research_context()
    return rc.research_context_version if rc is not None else None


def list_research_context_versions() -> list[ResearchContext]:
    """All revisions, oldest first."""
    with session() as s:
        rows = list(
            s.exec(
                select(ResearchContext).order_by(
                    ResearchContext.research_context_version.asc()  # type: ignore[union-attr]
                )
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return rows


def clear_research_context() -> bool:
    """Wipe research-context history. Returns ``True`` if rows were
    removed.

    NB: fresh-DB policy — codes / queue rows / theme runs that reference
    a research_context row will still carry their (now-dangling) FK
    value. Callers should only use this on a DB with no dependent rows.
    """
    with session() as s:
        rows = s.exec(select(ResearchContext)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows) > 0
