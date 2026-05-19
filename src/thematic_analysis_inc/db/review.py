"""Reviewer (system coder -1) helpers."""

from __future__ import annotations

from sqlalchemy import func
from sqlmodel import select

from thematic_analysis_inc.db.codebook import (
    copy_codebook_membership,
    insert_codebook_version,
)
from thematic_analysis_inc.db.coders import SYSTEM_REVIEWER_ID
from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    CodebookCode,
    CodesDerived,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_UPDATE,
    DERIVATION_REVIEW,
)
from thematic_analysis_inc.db.research_context import (
    latest_research_context_version,
)


def next_aggregated_code_to_review() -> Code | None:
    """An aggregator Code (coder_id=0) with no outgoing 'R' edge."""
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
            .where(Code.coder_id == 0, ~outgoing_r)
            .order_by(Code.code_id)
            .limit(1)
        ).first()
        if row is not None:
            s.expunge(row)
        return row


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


def record_review(
    *,
    source_agg_code: Code,
    decision: str,
    new_code_text: str,
    new_description: str,
    rationale: str,
    parent_codebook: Codebook,
    target_code: Code | None = None,
) -> Codebook:
    """Persist a non-SKIP review decision; returns the new Codebook."""
    if decision not in {DECISION_ADD, DECISION_MERGE, DECISION_UPDATE}:
        raise ValueError(f"invalid decision: {decision!r}")
    if decision in (DECISION_MERGE, DECISION_UPDATE) and target_code is None:
        raise ValueError(f"decision {decision!r} requires target_code")

    rc_version = latest_research_context_version()
    with session() as s:
        new_code = Code(
            segment_id=source_agg_code.segment_id,
            coder_id=SYSTEM_REVIEWER_ID,
            codebook_used_id=parent_codebook.version,
            research_context_used_id=rc_version,
            code=new_code_text,
            description=new_description or "",
            rationale=rationale or "",
        )
        s.add(new_code)
        s.commit()
        s.refresh(new_code)
        new_code_id = new_code.code_id

        s.add(
            CodesDerived(
                new_code_id=new_code_id,
                source_code_id=source_agg_code.code_id,
                derivation_type=DERIVATION_REVIEW,
                decision=decision,
                rationale=rationale,
            )
        )
        s.commit()
        s.expunge_all()
        # Re-read new_code as a fresh detached instance for the caller.
        new_code = Code(
            code_id=new_code_id,
            segment_id=source_agg_code.segment_id,
            coder_id=SYSTEM_REVIEWER_ID,
            codebook_used_id=parent_codebook.version,
            research_context_used_id=rc_version,
            code=new_code_text,
            description=new_description or "",
            rationale=rationale or "",
        )

    # New codebook revision (in a fresh session via the helper).
    new_cb = insert_codebook_version(parent=parent_codebook)

    if decision == DECISION_ADD:
        copy_codebook_membership(
            from_codebook=parent_codebook,
            to_codebook=new_cb,
            add_code=new_code,
        )
    elif decision == DECISION_MERGE:
        copy_codebook_membership(
            from_codebook=parent_codebook, to_codebook=new_cb
        )
    else:  # UPDATE
        copy_codebook_membership(
            from_codebook=parent_codebook,
            to_codebook=new_cb,
            drop_code=target_code,
            add_code=new_code,
        )
    return new_cb


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
                    ~outgoing_r,
                )
            ).one()
        )
