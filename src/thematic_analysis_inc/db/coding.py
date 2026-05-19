"""Coding queue + Stage-A coder code authoring.

The queue is explicit: nothing is added automatically when a document or
segment is created. Callers (CLI ``ta enqueue``, web ``/api/.../enqueue``)
must invoke :func:`enqueue_document`, :func:`enqueue_segment`, or
:func:`enqueue_pairs` to schedule work.

Each queue entry is keyed by ``(segment_id, coder_id, codebook_version,
research_context_version)``. Enqueueing at the *same* revisions for an
existing entry is a no-op; if either revision has moved forward, a fresh
entry is created so the worker re-codes the segment against the new
revision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

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


def _latest_codebook_version() -> int | None:
    with session() as s:
        cb = s.exec(
            select(Codebook).order_by(Codebook.version.desc()).limit(1)  # type: ignore[union-attr]
        ).first()
        return cb.version if cb is not None else None


def _all_real_coder_ids() -> list[int]:
    with session() as s:
        return list(
            s.exec(
                select(Coder.coder_id).where(Coder.coder_id >= 1)  # type: ignore[arg-type]
            ).all()
        )


def enqueue_pairs(
    pairs: Iterable[tuple[int, int]],
    *,
    codebook_version: int | None = None,
    research_context_version: int | None = None,
) -> int:
    """Insert a queue entry for each ``(segment_id, coder_id)`` pair at the
    given (or latest) codebook + research-context revisions. Returns the
    number of rows newly inserted; existing entries at the same revisions
    are silently skipped (idempotent)."""
    if codebook_version is None:
        codebook_version = _latest_codebook_version()
    if codebook_version is None:
        raise RuntimeError("no codebook revision exists; run init first")
    if research_context_version is None:
        research_context_version = latest_research_context_version()
    if research_context_version is None:
        raise RuntimeError(
            "no research-context revision exists; run init first"
        )
    inserted = 0
    now = _utcnow()
    with session() as s:
        for segment_id, coder_id in pairs:
            existing = s.get(
                CodingQueueEntry,
                (segment_id, coder_id, codebook_version, research_context_version),
            )
            if existing is not None:
                continue
            s.add(
                CodingQueueEntry(
                    segment_id=segment_id,
                    coder_id=coder_id,
                    codebook_used_id=codebook_version,
                    research_context_used_id=research_context_version,
                    enqueued_at=now,
                )
            )
            inserted += 1
        if inserted:
            s.commit()
    return inserted


def enqueue_document(
    document_id: int,
    coder_ids: Iterable[int] | None = None,
) -> int:
    """Schedule every segment in ``document_id`` for the given coders
    (default: all registered real coders) at the latest codebook and
    research-context revisions. Returns rows inserted."""
    coders = list(coder_ids) if coder_ids is not None else _all_real_coder_ids()
    if not coders:
        return 0
    with session() as s:
        seg_ids = list(
            s.exec(
                select(Segment.segment_id).where(  # type: ignore[arg-type]
                    Segment.document_id == document_id
                )
            ).all()
        )
    if not seg_ids:
        return 0
    return enqueue_pairs(
        (sid, cid) for sid in seg_ids for cid in coders
    )


def enqueue_segment(
    segment_id: int,
    coder_ids: Iterable[int] | None = None,
) -> int:
    """Schedule a single segment for the given coders (default: all real
    coders) at the latest codebook + research-context revisions."""
    coders = list(coder_ids) if coder_ids is not None else _all_real_coder_ids()
    if not coders:
        return 0
    return enqueue_pairs((segment_id, cid) for cid in coders)


def pending_count(coder: Coder | None = None) -> int:
    with session() as s:
        stmt = (
            select(func.count())
            .select_from(CodingQueueEntry)
            .where(
                CodingQueueEntry.claimed_at.is_(None),  # type: ignore[union-attr]
                CodingQueueEntry.finished_at.is_(None),  # type: ignore[union-attr]
                CodingQueueEntry.error.is_(None),  # type: ignore[union-attr]
            )
        )
        if coder is not None:
            stmt = stmt.where(CodingQueueEntry.coder_id == coder.coder_id)
        return int(s.exec(stmt).one())


def assignment_has_codes(assignment: CodingQueueEntry) -> bool:
    """True iff Code rows already exist for this assignment's
    (segment_id, coder_id, codebook_used_id, research_context_used_id)."""
    with session() as s:
        n = s.exec(
            select(func.count())
            .select_from(Code)
            .where(
                Code.segment_id == assignment.segment_id,
                Code.coder_id == assignment.coder_id,
                Code.codebook_used_id == assignment.codebook_used_id,
                Code.research_context_used_id
                == assignment.research_context_used_id,
            )
        ).one()
        return int(n) > 0


def mark_assignment_finished(assignment: CodingQueueEntry) -> None:
    """Mark a queue entry finished without recording new codes."""
    with session() as s:
        a = s.get(CodingQueueEntry, _assignment_pk(assignment))
        if a is None:
            return
        a.finished_at = _utcnow()
        s.add(a)
        s.commit()


def claim_next_assignment(coder: Coder | None = None) -> CodingQueueEntry | None:
    """Atomically claim the next pending row.

    If ``coder`` is given, restrict to that coder; otherwise claim any
    pending row across all coders.
    """
    while True:
        with session() as s:
            stmt = select(CodingQueueEntry).where(
                CodingQueueEntry.claimed_at.is_(None),  # type: ignore[union-attr]
                CodingQueueEntry.finished_at.is_(None),  # type: ignore[union-attr]
                CodingQueueEntry.error.is_(None),  # type: ignore[union-attr]
            )
            if coder is not None:
                stmt = stmt.where(CodingQueueEntry.coder_id == coder.coder_id)
            row = s.exec(
                stmt.order_by(CodingQueueEntry.segment_id).limit(1)
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


def _assignment_pk(a: CodingQueueEntry) -> tuple[int, int, int, int | None]:
    return (
        a.segment_id,
        a.coder_id,
        a.codebook_used_id,
        a.research_context_used_id,
    )


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
        a = s.get(CodingQueueEntry, _assignment_pk(assignment))
        if a is None:
            raise RuntimeError(
                f"assignment {_assignment_pk(assignment)} not found"
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
        a = s.get(CodingQueueEntry, _assignment_pk(assignment))
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
        a = s.get(CodingQueueEntry, _assignment_pk(assignment))
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
    """Return the most recent queue entry for ``(segment_id, coder_id)``
    (across all codebook + research-context revisions), or ``None`` if no
    entry exists. Used by the web UI to show a per-coder status badge."""
    with session() as s:
        q = s.exec(
            select(CodingQueueEntry)
            .where(
                CodingQueueEntry.segment_id == segment_id,
                CodingQueueEntry.coder_id == coder_id,
            )
            .order_by(
                CodingQueueEntry.enqueued_at.desc(),  # type: ignore[union-attr]
            )
            .limit(1)
        ).first()
        if q is not None:
            s.expunge(q)
        return q


def list_queue_entries_for_segment(
    segment_id: int,
) -> list[CodingQueueEntry]:
    """Return the latest queue entry per coder for ``segment_id``.

    Used by the web UI so it can distinguish "no coder has run yet" from
    "coder finished with zero codes": the latter has a queue row but no
    ``Code`` rows.
    """
    with session() as s:
        rows = list(
            s.exec(
                select(CodingQueueEntry)
                .where(CodingQueueEntry.segment_id == segment_id)
                .order_by(
                    CodingQueueEntry.coder_id,
                    CodingQueueEntry.enqueued_at.desc(),  # type: ignore[union-attr]
                )
            ).all()
        )
        latest: dict[int, CodingQueueEntry] = {}
        for r in rows:
            if r.coder_id not in latest:
                latest[r.coder_id] = r
        out = list(latest.values())
        for r in out:
            s.expunge(r)
        return out


def list_queue_entries_for_pair(
    segment_id: int, coder_id: int
) -> list[CodingQueueEntry]:
    """All queue entries for a (segment, coder) pair, newest first."""
    with session() as s:
        rows = list(
            s.exec(
                select(CodingQueueEntry)
                .where(
                    CodingQueueEntry.segment_id == segment_id,
                    CodingQueueEntry.coder_id == coder_id,
                )
                .order_by(
                    CodingQueueEntry.enqueued_at.desc(),  # type: ignore[union-attr]
                )
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return rows


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
