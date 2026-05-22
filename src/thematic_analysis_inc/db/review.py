"""Reviewer (system coder -1) helpers."""

from __future__ import annotations

from sqlalchemy import func
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
    SENTINEL_CODE_LABEL,
)


def next_aggregated_code_to_review() -> Code | None:
    """An aggregator Code (coder_id=0) with no outgoing 'R' edge,
    with ``supporting_quotes`` eager-loaded so the reviewer can read them
    without a session."""
    with session() as s:
        outgoing_r = (
            select(CodesDerived.source_code_id)
            .where(
                CodesDerived.source_code_id == Code.code_id,
                CodesDerived.derivation_type == DERIVATION_REVIEW,
            )
            .exists()
        )
        row = s.exec(
            select(Code)
            .where(
                Code.coder_id == 0,
                Code.code != SENTINEL_CODE_LABEL,
                ~outgoing_r,
            )
            .options(selectinload(Code.supporting_quotes))
            .order_by(Code.code_id)
            .limit(1)
        ).first()
        if row is not None:
            _ = list(row.supporting_quotes)
            s.expunge_all()
        return row


def pending_review_count() -> int:
    """Count aggregator Codes (coder_id=0) without an outgoing 'R' edge."""
    with session() as s:
        outgoing_r = (
            select(CodesDerived.source_code_id)
            .where(
                CodesDerived.source_code_id == Code.code_id,
                CodesDerived.derivation_type == DERIVATION_REVIEW,
            )
            .exists()
        )
        return s.exec(
            select(func.count())
            .select_from(Code)
            .where(
                Code.coder_id == 0,
                Code.code != SENTINEL_CODE_LABEL,
                ~outgoing_r,
            )
        ).one()


def resolve_target_code(codebook: Codebook, code_text: str) -> Code | None:
    """Reviewer Code with given text that belongs to ``codebook``."""
    with session() as s:
        c = s.exec(
            select(Code)
            .join(CodebookCode, CodebookCode.code_id == Code.code_id)
            .where(
                CodebookCode.codebook_version == codebook.version,
                Code.code == code_text,
            )
            .order_by(Code.code_id.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        if c is not None:
            s.expunge(c)
        return c


def save_reviewer_decision(code: Code) -> Code:
    """Persist a reviewer ``Code`` returned by ``ReviewerAgent.review_code``.

    The code is a fresh row (``code_id is None``); its ``supporting_quotes``
    are existing ``Quote`` rows that must be merged in, and its
    ``derivation_sources`` carry one or two ``CodesDerived`` edges whose
    ``source_code`` is an existing Code (aggregator code, or the previous
    reviewer target). Cascade writes the edge rows after ``s.add``.

    Returns a fresh detached ``Code`` with ``code_id`` populated and its
    ``derivation_sources`` / ``supporting_quotes`` re-hydrated.
    """
    if code.code_id is not None:
        raise ValueError(
            "save_reviewer_decision expects a fresh Code with code_id=None"
        )
    with session() as s:
        # Merge the caller's detached supporting_quotes / source_code
        # references into the session under no_autoflush. Without
        # no_autoflush, autoflush during s.merge tries to back-populate
        # Quote.codes onto the not-yet-added code and trips a SAWarning.
        # We deliberately keep s.add(code) AFTER the merges so the
        # detached source Code (target) never gets attached via cascade —
        # otherwise commit would expire it and callers reading its
        # column attrs would hit DetachedInstanceError.
        with s.no_autoflush:
            code.supporting_quotes = [
                s.merge(q) if q.quote_id is not None else q
                for q in (code.supporting_quotes or [])
            ]
            for edge in code.derivation_sources or []:
                if edge.source_code is not None and edge.source_code.code_id is not None:
                    edge.source_code = s.merge(edge.source_code)
        s.add(code)
        s.commit()
        s.refresh(code)
        _ = list(code.supporting_quotes)
        _ = list(code.derivation_sources)
        s.expunge_all()
        return code


def find_reviewer_code_by_text(code_text: str) -> Code | None:
    with session() as s:
        c = s.exec(
            select(Code)
            .where(Code.code == code_text, Code.coder_id == SYSTEM_REVIEWER_ID)
            .order_by(Code.code_id.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        if c is not None:
            s.expunge(c)
        return c


def list_review_decisions(
    decision: str | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[CodesDerived]]:
    with session() as s:
        stmt = select(CodesDerived).where(
            CodesDerived.derivation_type == DERIVATION_REVIEW
        )
        count_stmt = (
            select(func.count())
            .select_from(CodesDerived)
            .where(CodesDerived.derivation_type == DERIVATION_REVIEW)
        )
        if decision is not None:
            stmt = stmt.where(CodesDerived.decision == decision)
            count_stmt = count_stmt.where(CodesDerived.decision == decision)
        total = int(s.exec(count_stmt).one())
        rows = list(
            s.exec(
                stmt.order_by(CodesDerived.new_code_id.desc())  # type: ignore[union-attr]
                .limit(limit)
                .offset(offset)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return total, rows


def segment_review_remaining(segment_id: int) -> int:
    """Aggregator codes on this segment without an outgoing 'R' edge."""
    with session() as s:
        outgoing_r = (
            select(CodesDerived.source_code_id)
            .where(
                CodesDerived.source_code_id == Code.code_id,
                CodesDerived.derivation_type == DERIVATION_REVIEW,
            )
            .exists()
        )
        return int(
            s.exec(
                select(func.count())
                .select_from(Code)
                .where(
                    Code.segment_id == segment_id,
                    Code.coder_id == 0,
                    Code.code != SENTINEL_CODE_LABEL,
                    ~outgoing_r,
                )
            ).one()
        )
