"""Coding queue + Stage-A coder code authoring."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import select

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    Coder,
    CodesSupportingQuotes,
    CodingQueueEntry,
    Quote,
    Segment,
)
from thematic_analysis_inc.db.research_context import (
    latest_research_context_version,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def sync_coding_queue() -> int:
    """Insert a ``CodingQueueEntry`` for every (Segment, real-Coder) pair at
    the latest Codebook revision. Returns rows inserted."""
    with session() as s:
        cb = s.exec(
            select(Codebook).order_by(Codebook.version.desc()).limit(1)  # type: ignore[union-attr]
        ).first()
        if cb is None:
            return 0
        rc_version = latest_research_context_version()
        # Existing pairs.
        existing = {
            (q.segment_id, q.coder_id)
            for q in s.exec(select(CodingQueueEntry)).all()
        }
        seg_ids = list(
            s.exec(select(Segment.segment_id)).all()  # type: ignore[arg-type]
        )
        coder_ids = list(
            s.exec(
                select(Coder.coder_id).where(Coder.coder_id >= 1)  # type: ignore[arg-type]
            ).all()
        )
        inserted = 0
        for sid in seg_ids:
            for cid in coder_ids:
                if (sid, cid) in existing:
                    continue
                s.add(
                    CodingQueueEntry(
                        segment_id=sid,
                        coder_id=cid,
                        codebook_used_id=cb.version,
                        research_context_used_id=rc_version,
                    )
                )
                inserted += 1
        if inserted:
            s.commit()
        return inserted


def pending_count(coder: Coder) -> int:
    with session() as s:
        return int(
            s.exec(
                select(func.count())
                .select_from(CodingQueueEntry)
                .where(
                    CodingQueueEntry.coder_id == coder.coder_id,
                    CodingQueueEntry.claimed_at.is_(None),  # type: ignore[union-attr]
                    CodingQueueEntry.finished_at.is_(None),  # type: ignore[union-attr]
                    CodingQueueEntry.error.is_(None),  # type: ignore[union-attr]
                )
            ).one()
        )


def claim_next_assignment(coder: Coder) -> CodingQueueEntry | None:
    """Atomically claim the next pending row for ``coder``."""
    while True:
        with session() as s:
            row = s.exec(
                select(CodingQueueEntry)
                .where(
                    CodingQueueEntry.coder_id == coder.coder_id,
                    CodingQueueEntry.claimed_at.is_(None),  # type: ignore[union-attr]
                    CodingQueueEntry.finished_at.is_(None),  # type: ignore[union-attr]
                    CodingQueueEntry.error.is_(None),  # type: ignore[union-attr]
                )
                .order_by(CodingQueueEntry.segment_id)
                .limit(1)
            ).first()
            if row is None:
                return None
            # Conditional UPDATE for atomic claim — re-fetch and check.
            now_ts = _utcnow()
            row.claimed_at = now_ts
            s.add(row)
            try:
                s.commit()
            except Exception:
                s.rollback()
                continue
            s.refresh(row)
            # Load segment eagerly so caller can use row.segment.
            _ = row.segment.content  # noqa: B018
            s.expunge_all()
            return row


def record_coding_result(
    assignment: CodingQueueEntry, codes: list[Code]
) -> list[Code]:
    """Persist the coder's transient ``Code`` rows for this assignment.

    Each input ``Code`` should carry ``code`` and ``description``, plus
    any number of transient ``Quote`` instances on
    ``code.supporting_quotes`` (their ``text`` is read; ``segment_id``
    is set here). Inserts ``Quote`` rows and ``codes_supporting_quotes``
    link rows alongside each Code. Marks the queue entry done.
    """
    with session() as s:
        a = s.get(
            CodingQueueEntry, (assignment.segment_id, assignment.coder_id)
        )
        if a is None:
            raise RuntimeError(
                f"assignment ({assignment.segment_id}, "
                f"{assignment.coder_id}) not found"
            )
        out: list[Code] = []
        for src in codes:
            quote_texts = [
                q.text for q in (src.supporting_quotes or []) if q.text
            ]
            c = Code(
                segment_id=a.segment_id,
                coder_id=a.coder_id,
                codebook_used_id=a.codebook_used_id,
                research_context_used_id=a.research_context_used_id,
                code=src.code,
                description=src.description or "",
                rationale="",
            )
            s.add(c)
            s.flush()  # assign code_id
            for qt in quote_texts:
                q = Quote(segment_id=a.segment_id, text=qt)
                s.add(q)
                s.flush()  # assign quote_id
                s.add(
                    CodesSupportingQuotes(code_id=c.code_id, quote_id=q.quote_id)
                )
            out.append(c)
        a.finished_at = _utcnow()
        s.add(a)
        s.commit()
        for c in out:
            s.refresh(c)
            s.expunge(c)
        return out


def record_coding_failure(
    assignment: CodingQueueEntry, error: str
) -> None:
    with session() as s:
        a = s.get(
            CodingQueueEntry, (assignment.segment_id, assignment.coder_id)
        )
        if a is None:
            return
        a.error = error
        a.finished_at = _utcnow()
        s.add(a)
        s.commit()


def reset_assignment(
    assignment: CodingQueueEntry, *, force: bool = False
) -> None:
    """Drop this coder's codes for the segment and clear the queue
    entry's claim/finish/error. ``force`` is accepted for symmetry with
    other cascades but has no extra effect here."""
    from thematic_analysis_inc.db.cascades import (
        _delete_codes_and_dependents,
    )

    with session() as s:
        a = s.get(
            CodingQueueEntry, (assignment.segment_id, assignment.coder_id)
        )
        if a is None:
            return
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


def load_segment_coder_codes(segment: Segment) -> dict[int, list[Code]]:
    """Map coder_id → list[Code] for real coders only (id ≥ 1).

    Eagerly loads each code's ``supporting_quotes`` so callers (e.g.
    the aggregator) can read them after the session closes.
    """
    from sqlalchemy.orm import selectinload

    with session() as s:
        rows = list(
            s.exec(
                select(Code)
                .where(Code.segment_id == segment.segment_id, Code.coder_id >= 1)
                .options(selectinload(Code.supporting_quotes))  # type: ignore[arg-type]
                .order_by(Code.coder_id, Code.code_id)
            ).all()
        )
        # Touch the relationship before expunging so it's materialised.
        for r in rows:
            _ = list(r.supporting_quotes)
        for r in rows:
            s.expunge(r)
        out: dict[int, list[Code]] = {}
        for c in rows:
            out.setdefault(c.coder_id, []).append(c)
        return out


def edit_code_text(code: Code, new_text: str) -> bool:
    with session() as s:
        c = s.get(Code, code.code_id)
        if c is None:
            return False
        c.code = new_text
        s.add(c)
        s.commit()
        return True


def list_queue_entries(
    coder: Coder | None = None, *, limit: int = 100, offset: int = 0
) -> tuple[int, list[CodingQueueEntry]]:
    with session() as s:
        stmt = select(CodingQueueEntry)
        count_stmt = select(func.count()).select_from(CodingQueueEntry)
        if coder is not None:
            stmt = stmt.where(CodingQueueEntry.coder_id == coder.coder_id)
            count_stmt = count_stmt.where(
                CodingQueueEntry.coder_id == coder.coder_id
            )
        total = int(s.exec(count_stmt).one())
        rows = list(
            s.exec(
                stmt.order_by(
                    CodingQueueEntry.segment_id.desc(),  # type: ignore[union-attr]
                    CodingQueueEntry.coder_id,
                )
                .limit(limit)
                .offset(offset)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return total, rows


def get_queue_entry(
    segment_id: int, coder_id: int
) -> CodingQueueEntry | None:
    with session() as s:
        q = s.get(CodingQueueEntry, (segment_id, coder_id))
        if q is not None:
            s.expunge(q)
        return q


def coder_progress(coder: Coder) -> dict[str, int]:
    """Counts by derived status for this coder's queue rows."""
    out = {"done": 0, "running": 0, "failed": 0, "pending": 0}
    with session() as s:
        rows = list(
            s.exec(
                select(CodingQueueEntry).where(
                    CodingQueueEntry.coder_id == coder.coder_id
                )
            ).all()
        )
    for r in rows:
        out[r.status] = out.get(r.status, 0) + 1
    return out


def reset_failed_assignments(coder: Coder) -> int:
    """Clear error + claim/finish on this coder's failed queue rows."""
    with session() as s:
        rows = list(
            s.exec(
                select(CodingQueueEntry).where(
                    CodingQueueEntry.coder_id == coder.coder_id,
                    CodingQueueEntry.error.is_not(None),  # type: ignore[union-attr]
                )
            ).all()
        )
        for r in rows:
            r.claimed_at = None
            r.finished_at = None
            r.error = None
            s.add(r)
        s.commit()
        return len(rows)


def reset_all_assignments(coder: Coder) -> int:
    """Drop this coder's codes and clear all their queue rows."""
    from thematic_analysis_inc.db.cascades import (
        _delete_codes_and_dependents,
    )

    with session() as s:
        code_ids = list(s.exec(
            select(Code.code_id).where(Code.coder_id == coder.coder_id)  # type: ignore[arg-type]
        ).all())
        _delete_codes_and_dependents(s, code_ids)
        rows = list(
            s.exec(
                select(CodingQueueEntry).where(
                    CodingQueueEntry.coder_id == coder.coder_id
                )
            ).all()
        )
        for r in rows:
            r.claimed_at = None
            r.finished_at = None
            r.error = None
            s.add(r)
        s.commit()
        return len(rows)
