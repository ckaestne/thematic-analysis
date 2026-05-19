"""Cascade-delete helpers, SQLModel-backed.

These helpers walk the same FK structure the schema declares (no
``ON DELETE CASCADE`` magic — we do it explicitly so the order is
predictable). Most take SQLModel object arguments.
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlmodel import Session, select

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    CodebookCode,
    Coder,
    CodesDerived,
    CodesSupportingQuotes,
    CodingQueueEntry,
    Document,
    Quote,
    Segment,
    DERIVATION_REVIEW,
)


# ---------------------------------------------------------------------------
# Low-level helpers (take an open session)
# ---------------------------------------------------------------------------


def _delete_codes_and_dependents(s: Session, code_ids: list[int]) -> None:
    if not code_ids:
        return
    s.exec(  # type: ignore[call-overload]
        delete(CodesDerived).where(
            (CodesDerived.new_code_id.in_(code_ids))  # type: ignore[attr-defined]
            | (CodesDerived.source_code_id.in_(code_ids))  # type: ignore[attr-defined]
        )
    )
    s.exec(  # type: ignore[call-overload]
        delete(CodesSupportingQuotes).where(
            CodesSupportingQuotes.code_id.in_(code_ids)  # type: ignore[attr-defined]
        )
    )
    s.exec(  # type: ignore[call-overload]
        delete(CodebookCode).where(
            CodebookCode.code_id.in_(code_ids)  # type: ignore[attr-defined]
        )
    )
    s.exec(  # type: ignore[call-overload]
        delete(Code).where(Code.code_id.in_(code_ids))  # type: ignore[attr-defined]
    )


def _delete_quotes_for_segment(s: Session, segment_id: int) -> None:
    qids = list(s.exec(
            select(Quote.quote_id).where(Quote.segment_id == segment_id)  # type: ignore[arg-type]
        ).all())
    if not qids:
        return
    s.exec(  # type: ignore[call-overload]
        delete(CodesSupportingQuotes).where(
            CodesSupportingQuotes.quote_id.in_(qids)  # type: ignore[attr-defined]
        )
    )
    s.exec(  # type: ignore[call-overload]
        delete(Quote).where(Quote.quote_id.in_(qids))  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# Public cascades (take SQLModel objects)
# ---------------------------------------------------------------------------


def delete_segment_cascade(segment: Segment) -> bool:
    """Delete a Segment and every row that points back at it."""
    with session() as s:
        seg = s.get(Segment, segment.segment_id)
        if seg is None:
            return False
        code_ids = list(s.exec(
                select(Code.code_id).where(  # type: ignore[arg-type]
                    Code.segment_id == seg.segment_id
                )
            ).all())
        _delete_codes_and_dependents(s, code_ids)
        _delete_quotes_for_segment(s, seg.segment_id)
        s.exec(  # type: ignore[call-overload]
            delete(CodingQueueEntry).where(
                CodingQueueEntry.segment_id == seg.segment_id
            )
        )
        s.delete(seg)
        s.commit()
        return True


def delete_document_cascade(document: Document) -> tuple[bool, int]:
    """Delete a Document and every Segment it owns (with cascades)."""
    with session() as s:
        d = s.get(Document, document.document_id)
        if d is None:
            return False, 0
        seg_ids = list(s.exec(
                select(Segment.segment_id).where(  # type: ignore[arg-type]
                    Segment.document_id == d.document_id
                )
            ).all())
        for sid in seg_ids:
            code_ids = list(s.exec(
                    select(Code.code_id).where(Code.segment_id == sid)  # type: ignore[arg-type]
                ).all())
            _delete_codes_and_dependents(s, code_ids)
            _delete_quotes_for_segment(s, sid)
            s.exec(  # type: ignore[call-overload]
                delete(CodingQueueEntry).where(
                    CodingQueueEntry.segment_id == sid
                )
            )
            s.exec(  # type: ignore[call-overload]
                delete(Segment).where(Segment.segment_id == sid)
            )
        s.delete(d)
        s.commit()
        return True, len(seg_ids)


def delete_coder_cascade(
    coder: Coder, *, force: bool = False
) -> tuple[bool, int]:
    """Delete a real Coder and (optionally) all their queue rows."""
    if coder.coder_id <= 0:
        raise ValueError(
            f"refusing to delete system coder (coder_id={coder.coder_id})"
        )
    with session() as s:
        c = s.get(Coder, coder.coder_id)
        if c is None:
            return False, 0
        n = int(
            s.exec(
                select(CodingQueueEntry).where(
                    CodingQueueEntry.coder_id == coder.coder_id
                )
            ).all().__len__()
        )
        if n > 0 and not force:
            raise RuntimeError(
                f"coder {coder.coder_id} has {n} coding_queue rows; "
                "pass force=True to cascade"
            )
        code_ids = list(s.exec(
                select(Code.code_id).where(Code.coder_id == coder.coder_id)  # type: ignore[arg-type]
            ).all())
        _delete_codes_and_dependents(s, code_ids)
        n_q = int(
            s.exec(  # type: ignore[call-overload]
                delete(CodingQueueEntry).where(
                    CodingQueueEntry.coder_id == coder.coder_id
                )
            ).rowcount or 0
        )
        s.delete(c)
        s.commit()
        return True, n_q


def delete_aggregation_for_segment_cascade(segment: Segment) -> int:
    """Delete every aggregator code (and downstream reviewer codes) for a
    segment. Returns count of aggregator codes deleted."""
    with session() as s:
        agg_ids = list(s.exec(
                select(Code.code_id).where(  # type: ignore[arg-type]
                    Code.segment_id == segment.segment_id,
                    Code.coder_id == 0,
                )
            ).all())
        rev_ids: list[int] = []
        if agg_ids:
            rev_ids = list(s.exec(
                    select(CodesDerived.new_code_id).where(  # type: ignore[arg-type]
                        CodesDerived.source_code_id.in_(agg_ids),  # type: ignore[attr-defined]
                        CodesDerived.derivation_type == DERIVATION_REVIEW,
                    )
                ).all())
        _delete_codes_and_dependents(s, rev_ids)
        _delete_codes_and_dependents(s, agg_ids)
        # Drop any now-orphaned quotes on this segment.
        s.exec(  # type: ignore[call-overload]
            delete(Quote).where(
                Quote.segment_id == segment.segment_id,
                ~Quote.quote_id.in_(  # type: ignore[attr-defined]
                    select(CodesSupportingQuotes.quote_id)
                ),
            )
        )
        s.commit()
        return len(agg_ids)


def reset_coding_assignment_cascade(
    assignment: CodingQueueEntry, *, force: bool = False
) -> bool:
    """Drop this coder's codes for the segment, clear the queue claim,
    and cascade-delete any aggregator/reviewer codes for the segment."""
    with session() as s:
        a = s.get(
            CodingQueueEntry,
            (
                assignment.segment_id,
                assignment.coder_id,
                assignment.codebook_used_id,
                assignment.research_context_used_id,
            ),
        )
        if a is None:
            return False
        code_ids = list(s.exec(
                select(Code.code_id).where(  # type: ignore[arg-type]
                    Code.segment_id == a.segment_id,
                    Code.coder_id == a.coder_id,
                )
            ).all())
        _delete_codes_and_dependents(s, code_ids)
        a.claimed_at = None
        a.finished_at = None
        a.error = None
        s.add(a)
        s.commit()
        # Now nuke downstream aggregator/reviewer codes for the segment.
        seg = Segment(segment_id=a.segment_id)  # only id needed
        delete_aggregation_for_segment_cascade(seg)
        return True
