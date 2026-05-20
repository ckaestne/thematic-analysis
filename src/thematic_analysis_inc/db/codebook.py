"""Codebook revisions + membership + JSON snapshot serializer."""

from __future__ import annotations

import json

from sqlalchemy.orm import selectinload
from sqlmodel import select

from thematic_analysis_inc.db.coders import SYSTEM_REVIEWER_ID
from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    CodebookCode,
    CodesDerived,
    DERIVATION_REVIEW,
    Quote,
)


def insert_codebook_version(
    parent: Codebook | None = None,
    research_context_version: int | None = None,
) -> Codebook:
    """Insert a new Codebook revision; returns the persisted row.

    Every codebook revision pins exactly one research-context revision.
    ``research_context_version`` defaults to the latest revision in the
    table (callers that drive a review will normally let it default,
    since reviews don't change the research context). Raises if no
    research context exists yet.
    """
    if research_context_version is None:
        from thematic_analysis_inc.db.research_context import (
            latest_research_context_version,
        )

        research_context_version = latest_research_context_version()
    if research_context_version is None:
        raise RuntimeError(
            "no research-context revision exists; set one before "
            "creating a codebook revision"
        )
    with session() as s:
        cb = Codebook(
            parent_version=parent.version if parent is not None else None,
            research_context_version=research_context_version,
        )
        s.add(cb)
        s.commit()
        s.refresh(cb)
        s.expunge(cb)
        return cb


def latest_codebook() -> Codebook | None:
    with session() as s:
        cb = s.exec(
            select(Codebook)
            .order_by(Codebook.version.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        if cb is not None:
            s.expunge(cb)
        return cb


def get_codebook(version: int) -> Codebook | None:
    with session() as s:
        cb = s.get(Codebook, version)
        if cb is not None:
            s.expunge(cb)
        return cb


def get_codebook_with_codes_and_research_context(
    version: int,
) -> Codebook | None:
    """Like :func:`get_codebook`, but also eager-loads ``codebook.codes``
    (with their ``supporting_quotes``) and ``codebook.research_context``
    so agents bound to the returned (detached) instance can traverse
    them without a live session."""
    with session() as s:
        cb = s.exec(
            select(Codebook)
            .where(Codebook.version == version)
            .options(
                selectinload(Codebook.codes).selectinload(  # type: ignore[arg-type]
                    Code.supporting_quotes
                ),
                selectinload(Codebook.research_context),  # type: ignore[arg-type]
            )
        ).first()
        if cb is None:
            return None
        for c in cb.codes:
            _ = list(c.supporting_quotes)
        _ = cb.research_context
        s.expunge_all()
        return cb


def list_codebooks() -> list[Codebook]:
    with session() as s:
        rows = list(
            s.exec(select(Codebook).order_by(Codebook.version.asc())).all()  # type: ignore[union-attr]
        )
        for r in rows:
            s.expunge(r)
        return rows


def codebook_to_json_for_version(version: int) -> str:
    """Serialize codes-in-version as the legacy ``{"codes": [...]}`` shape
    that ``thematic_analysis.codebook.Codebook.from_json`` consumes."""
    with session() as s:
        codes = list(
            s.exec(
                select(Code)
                .join(CodebookCode, CodebookCode.code_id == Code.code_id)
                .where(CodebookCode.codebook_version == version)
                .order_by(Code.code_id)
            ).all()
        )
        out_codes: list[dict] = []
        for c in codes:
            quotes_payload = [
                {"quote_id": str(q.quote_id), "text": q.text}
                for q in sorted(
                    c.supporting_quotes, key=lambda x: x.quote_id
                )
            ]
            out_codes.append({"code": c.code, "quotes": quotes_payload})
        return json.dumps({"codes": out_codes}, indent=2)


def add_code_to_codebook(codebook: Codebook, code: Code) -> None:
    """Membership: place ``code`` into ``codebook`` (idempotent)."""
    with session() as s:
        existing = s.get(CodebookCode, (codebook.version, code.code_id))
        if existing is None:
            s.add(
                CodebookCode(
                    codebook_version=codebook.version, code_id=code.code_id
                )
            )
            s.commit()


def copy_codebook_membership(
    *,
    from_codebook: Codebook,
    to_codebook: Codebook,
    drop_codes: list[Code] | None = None,
    add_codes: list[Code] | None = None,
    drop_code: Code | None = None,  # legacy singular
    add_code: Code | None = None,  # legacy singular
) -> None:
    """Copy membership from one Codebook to another with bulk diff.

    ``drop_codes`` / ``add_codes``: lists used by the reviewer's batched
    finalize step. ``drop_code`` / ``add_code``: legacy singular kwargs
    kept for older callers (research-context revision creation).
    """
    drop_list = list(drop_codes or [])
    if drop_code is not None:
        drop_list.append(drop_code)
    add_list = list(add_codes or [])
    if add_code is not None:
        add_list.append(add_code)
    drop_ids = {c.code_id for c in drop_list if c.code_id is not None}
    add_ids = [c.code_id for c in add_list if c.code_id is not None]
    with session() as s:
        rows = list(
            s.exec(
                select(CodebookCode).where(
                    CodebookCode.codebook_version == from_codebook.version
                )
            ).all()
        )
        for r in rows:
            if r.code_id in drop_ids:
                continue
            existing = s.get(
                CodebookCode, (to_codebook.version, r.code_id)
            )
            if existing is None:
                s.add(
                    CodebookCode(
                        codebook_version=to_codebook.version,
                        code_id=r.code_id,
                    )
                )
        for cid in add_ids:
            existing = s.get(CodebookCode, (to_codebook.version, cid))
            if existing is None:
                s.add(
                    CodebookCode(
                        codebook_version=to_codebook.version, code_id=cid
                    )
                )
        s.commit()


def _batch_reviewer_codes(parent_version: int) -> list[Code]:
    """Reviewer-authored Codes (coder_id == -1) created in this batch.

    A code is "in this batch" if it has ``codebook_used_id == parent_version``
    and is the ``new_code`` end of an outgoing ``CodesDerived`` 'R' edge.
    Returned in ``code_id`` insertion order, with derivation edges and
    source codes eager-loaded so callers can walk provenance without a
    session.
    """
    with session() as s:
        rows = list(
            s.exec(
                select(Code)
                .where(
                    Code.coder_id == SYSTEM_REVIEWER_ID,
                    Code.codebook_used_id == parent_version,
                )
                .options(
                    selectinload(Code.derivation_sources).selectinload(
                        CodesDerived.source_code
                    ),
                    selectinload(Code.supporting_quotes),
                )
                .order_by(Code.code_id)
            ).all()
        )
        # Filter to those that actually have an 'R' edge (skip the rare
        # case of an orphan reviewer Code with no provenance).
        rows = [
            c
            for c in rows
            if any(
                e.derivation_type == DERIVATION_REVIEW
                for e in c.derivation_sources
            )
        ]
        for c in rows:
            _ = list(c.supporting_quotes)
            _ = list(c.derivation_sources)
        s.expunge_all()
        return rows


def live_codes_for_batch(parent: Codebook) -> list[Code]:
    """Effective codebook membership *during* a review batch.

    Starts from ``parent.codes``; applies each reviewer decision authored
    against ``parent.version`` in ``code_id`` insertion order:

    - decision 'A': add the new reviewer code.
    - decisions 'M' / 'U': add the new reviewer code, drop the previous
      target reviewer code (the second source on the 'R' edges — the one
      whose ``coder_id == -1``).

    The reviewer agent uses this set as its similarity-search pool so two
    new codes added earlier in the same batch are still visible to later
    reviews.
    """
    membership = {c.code_id: c for c in parent.codes}
    for new_code in _batch_reviewer_codes(parent.version):
        review_edges = [
            e
            for e in new_code.derivation_sources
            if e.derivation_type == DERIVATION_REVIEW
        ]
        prev_targets = [
            e.source_code
            for e in review_edges
            if e.source_code is not None
            and e.source_code.coder_id == SYSTEM_REVIEWER_ID
        ]
        for prev in prev_targets:
            membership.pop(prev.code_id, None)
        membership[new_code.code_id] = new_code
    return list(membership.values())


def materialize_codebook_revision(parent: Codebook) -> Codebook | None:
    """Create one new Codebook revision capturing every reviewer decision
    written against ``parent`` since ``parent`` was created.

    Returns the new ``Codebook`` row, or ``None`` if no decisions changed
    membership (in which case the caller prints "codebook unchanged").
    """
    parent_ids = {c.code_id for c in parent.codes}
    live = live_codes_for_batch(parent)
    live_ids = {c.code_id for c in live}
    if live_ids == parent_ids:
        return None

    new_cb = insert_codebook_version(parent=parent)
    add_codes = [c for c in live if c.code_id not in parent_ids]
    drop_codes = [c for c in parent.codes if c.code_id not in live_ids]
    copy_codebook_membership(
        from_codebook=parent,
        to_codebook=new_cb,
        drop_codes=drop_codes,
        add_codes=add_codes,
    )
    return new_cb
