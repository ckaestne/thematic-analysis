"""Aggregator (system coder 0) helpers."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import aliased
from sqlmodel import select

from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID
from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    CodesSupportingQuotes,
    CodingQueueEntry,
    Quote,
    Segment,
    SENTINEL_CODE_LABEL,
)


def next_segment_codebook_to_aggregate() -> tuple[Segment, int] | None:
    """Find the next (Segment, codebook_version) pair that needs aggregation.

    A pair qualifies when every queue entry for that (segment, codebook_version)
    is finished without error, at least one coder code exists at that version,
    and no aggregator code exists yet at that version.

    Codebook version is discovered from the database across all versions —
    the latest codebook is NOT assumed.
    """
    with session() as s:
        Q = aliased(CodingQueueEntry)
        QInner = aliased(CodingQueueEntry)

        has_unfinished = (
            select(QInner.segment_id)  # type: ignore[union-attr]
            .where(
                QInner.segment_id == Q.segment_id,
                QInner.codebook_used_id == Q.codebook_used_id,
                (QInner.finished_at.is_(None))  # type: ignore[union-attr]
                | (QInner.error.is_not(None)),  # type: ignore[union-attr]
            )
            .exists()
        )
        has_agg = (
            select(Code.code_id)
            .where(
                Code.segment_id == Q.segment_id,
                Code.coder_id == SYSTEM_AGGREGATOR_ID,
                Code.codebook_used_id == Q.codebook_used_id,
            )
            .exists()
        )
        has_coder_code = (
            select(Code.code_id)
            .where(
                Code.segment_id == Q.segment_id,
                Code.coder_id >= 1,
                Code.codebook_used_id == Q.codebook_used_id,
            )
            .exists()
        )
        row = s.exec(
            select(Q.segment_id, Q.codebook_used_id)  # type: ignore[union-attr]
            .where(~has_unfinished, ~has_agg, has_coder_code)
            .distinct()
            .order_by(Q.segment_id, Q.codebook_used_id)
            .limit(1)
        ).first()

        if row is None:
            return None
        segment_id, codebook_version = row

        seg = s.get(Segment, segment_id)
        if seg is None:
            return None
        _ = seg.content
        s.expunge(seg)
        return seg, codebook_version


def next_segment_to_aggregate() -> Segment | None:
    """Return the next Segment that needs aggregation, or None.

    Delegates to :func:`next_segment_codebook_to_aggregate` and discards
    the codebook version. Prefer the paired version in new code.
    """
    result = next_segment_codebook_to_aggregate()
    return result[0] if result is not None else None


def unaggregated_codebook_versions_for_segment(segment_id: int) -> list[int]:
    """Return codebook versions where the segment has coder codes but no
    aggregator code yet and all queue entries are finished without error.

    Results are ordered oldest-first so aggregation proceeds in
    chronological order.
    """
    with session() as s:
        Q = aliased(CodingQueueEntry)
        QInner = aliased(CodingQueueEntry)

        has_unfinished = (
            select(QInner.segment_id)  # type: ignore[union-attr]
            .where(
                QInner.segment_id == segment_id,
                QInner.codebook_used_id == Q.codebook_used_id,
                (QInner.finished_at.is_(None))  # type: ignore[union-attr]
                | (QInner.error.is_not(None)),  # type: ignore[union-attr]
            )
            .exists()
        )
        has_agg = (
            select(Code.code_id)
            .where(
                Code.segment_id == segment_id,
                Code.coder_id == SYSTEM_AGGREGATOR_ID,
                Code.codebook_used_id == Q.codebook_used_id,
            )
            .exists()
        )
        has_coder_code = (
            select(Code.code_id)
            .where(
                Code.segment_id == segment_id,
                Code.coder_id >= 1,
                Code.codebook_used_id == Q.codebook_used_id,
            )
            .exists()
        )
        rows = s.exec(
            select(Q.codebook_used_id)  # type: ignore[union-attr]
            .where(
                Q.segment_id == segment_id,
                ~has_unfinished,
                ~has_agg,
                has_coder_code,
            )
            .distinct()
            .order_by(Q.codebook_used_id)
        ).all()
        return list(rows)


def segment_has_aggregator_code(
    segment_id: int,
    codebook_version: int | None = None,
) -> bool:
    """Whether the segment already has any aggregator row. When
    ``codebook_version`` is given, only rows at that version count."""
    with session() as s:
        q = select(Code.code_id).where(
            Code.segment_id == segment_id,
            Code.coder_id == SYSTEM_AGGREGATOR_ID,
        )
        if codebook_version is not None:
            q = q.where(Code.codebook_used_id == codebook_version)
        return s.exec(q.limit(1)).first() is not None


def load_segment_and_codebook_for_aggregation(
    segment_id: int, codebook_version: int
) -> tuple[Segment, "Codebook"] | None:
    """Load a Segment (with codes + their supporting_quotes +
    ``code.codebook_used``) AND a specific Codebook in one session.

    Returning both from the same identity map is what lets the
    aggregator filter inputs with ``c.codebook_used is codebook``
    (see ``.claude/skills/sqlalchemy-identity``). Returns ``None`` if
    either row is missing.
    """
    from sqlalchemy.orm import selectinload

    from thematic_analysis_inc.db.models import Codebook

    with session() as s:
        seg = s.exec(
            select(Segment)
            .where(Segment.segment_id == segment_id)
            .options(
                selectinload(Segment.quotes),  # type: ignore[arg-type]
                selectinload(Segment.codes)  # type: ignore[arg-type]
                .selectinload(Code.supporting_quotes),  # type: ignore[arg-type]
                selectinload(Segment.codes)  # type: ignore[arg-type]
                .selectinload(Code.codebook_used),  # type: ignore[arg-type]
            )
        ).first()
        if seg is None:
            return None
        cb = s.get(Codebook, codebook_version)
        if cb is None:
            return None
        # Touch relationships so they survive expunge.
        _ = list(seg.codes)
        for c in seg.codes:
            _ = list(c.supporting_quotes)
            _ = c.codebook_used
        s.expunge_all()
        return seg, cb


def save_aggregator_codes(codes: list[Code]) -> list[Code]:
    """Persist new aggregator Codes returned by the agent.

    ``codes`` is the list produced by ``CodeAggregatorAgent.aggregate``.
    Each Code already carries ``segment_id`` / ``coder_id`` /
    ``codebook_used_id``; items with ``code_id`` set are retained
    originals (already in the DB) and are skipped. New items are added;
    their ``supporting_quotes`` and ``derivation_sources`` lists are
    wired through to existing rows and link/edge rows are inserted via
    cascade.

    The agent is responsible for emitting a sentinel ``Code`` when it
    saw inputs but produced no real codes; this helper just persists
    whatever it is handed.
    """
    with session() as s:
        written: list[Code] = []
        for c in codes:
            if c.code_id is not None:
                continue
            c.supporting_quotes = [
                s.merge(q) if q.quote_id is not None else q
                for q in (c.supporting_quotes or [])
            ]
            for edge in c.derivation_sources or []:
                if edge.source_code is not None and edge.source_code.code_id is not None:
                    edge.source_code = s.merge(edge.source_code)
            s.add(c)
            written.append(c)
        s.commit()
        out: list[Code] = []
        for c in written:
            s.refresh(c)
            _ = list(c.supporting_quotes)
            _ = list(c.derivation_sources)
            s.expunge(c)
            out.append(c)
        return out


def get_aggregator_code(code_id: int) -> Code | None:
    """Detached aggregator (``coder_id == 0``) ``Code`` row by id, or
    ``None`` if missing or not an aggregator code. ``supporting_quotes``
    is eager-loaded so callers can iterate quotes without a session."""
    from sqlalchemy.orm import selectinload

    with session() as s:
        c = s.exec(
            select(Code)
            .where(Code.code_id == code_id)
            .options(selectinload(Code.supporting_quotes))
        ).first()
        if c is None or c.coder_id != 0:
            return None
        _ = list(c.supporting_quotes)
        s.expunge(c)
        return c


def load_aggregated_code_quotes(code_id: int) -> list[dict]:
    """Return ``[{quote_id, text}, ...]`` for an aggregator code."""
    with session() as s:
        rows = list(
            s.exec(
                select(Quote)
                .join(
                    CodesSupportingQuotes,
                    CodesSupportingQuotes.quote_id == Quote.quote_id,
                )
                .where(CodesSupportingQuotes.code_id == code_id)
                .order_by(Quote.quote_id)
            ).all()
        )
    return [{"quote_id": q.quote_id, "text": q.text} for q in rows]


def list_aggregator_codes_for_segment(segment_id: int) -> list[Code]:
    """Aggregator codes for a segment, excluding the empty-aggregation
    sentinel row."""
    with session() as s:
        rows = list(
            s.exec(
                select(Code)
                .where(
                    Code.segment_id == segment_id,
                    Code.coder_id == SYSTEM_AGGREGATOR_ID,
                    Code.code != SENTINEL_CODE_LABEL,
                )
                .order_by(Code.code_id)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return rows


def list_aggregations(
    *, limit: int = 100, offset: int = 0
) -> tuple[int, list[dict]]:
    """List per-segment aggregations as a synthetic view over Code rows
    with ``coder_id = 0``. Returns ``(total, items)``."""
    with session() as s:
        total = int(
            s.exec(
                select(func.count(func.distinct(Code.segment_id))).where(
                    Code.coder_id == SYSTEM_AGGREGATOR_ID
                )
            ).one()
        )
        rows = list(
            s.exec(
                select(
                    Code.segment_id,
                    func.count().label("n_codes"),
                    func.min(Code.code_id).label("first_id"),
                )
                .where(Code.coder_id == SYSTEM_AGGREGATOR_ID)
                .group_by(Code.segment_id)
                .order_by(Code.segment_id.desc())  # type: ignore[union-attr]
                .limit(limit)
                .offset(offset)
            ).all()
        )
    items = [
        {"segment_id": r[0], "n_codes": r[1], "first_id": r[2]} for r in rows
    ]
    return total, items
