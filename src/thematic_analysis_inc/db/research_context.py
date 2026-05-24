"""Versioned research-context CRUD, backed by SQLModel.

Each call to :func:`add_research_context_and_codebook_revision`
inserts a new row keyed by an autoincrementing
``research_context_version`` *and* creates a new ``Codebook`` revision
pinned to it. The rest of the pipeline tracks only the codebook
version — the research context active for any codebook is reachable
via ``codebook.research_context``.

Callers receive SQLModel objects and use ``rc.description``,
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
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def add_research_context_and_codebook_revision(
    ctx: DomainResearchContext,
) -> ResearchContext:
    """Insert a new research-context revision AND a fresh ``Codebook``
    revision pinned to it — both in one logical write.

    Returns the persisted research-context row. The new codebook
    inherits its membership from the previous latest codebook (if any).
    History is preserved: previous revisions stay in the table so codes
    / queue rows / theme runs that reference earlier codebook versions
    remain valid.
    """
    # Lazy import to avoid a top-level cycle (codebook.py imports
    # latest_research_context_version from this module).
    from thematic_analysis_inc.db.codebook import (
        copy_codebook_membership,
        insert_codebook_version,
        latest_codebook,
    )

    rc = _from_domain(ctx)
    with session() as s:
        s.add(rc)
        s.commit()
        s.refresh(rc)
        new_version = rc.research_context_version
        s.expunge(rc)

    parent_cb = latest_codebook()
    new_cb = insert_codebook_version(
        parent=parent_cb, research_context_version=new_version
    )
    if parent_cb is not None:
        copy_codebook_membership(from_codebook=parent_cb, to_codebook=new_cb)
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
    """Wipe research-context history *and* every codebook revision that
    pinned one. Returns ``True`` if rows were removed.

    NB: fresh-DB policy — downstream rows (codes, queue) keep their
    codebook FK values, which will now be dangling. Callers should only
    use this on a DB with no dependent rows.
    """
    from thematic_analysis_inc.db.models import Codebook

    with session() as s:
        for cb in s.exec(select(Codebook)).all():
            s.delete(cb)
        rows = s.exec(select(ResearchContext)).all()
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows) > 0
