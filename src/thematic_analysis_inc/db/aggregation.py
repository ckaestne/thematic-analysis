"""Aggregator (system coder 0) helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import aliased
from sqlmodel import select

from thematic_analysis_inc.db.codebook import latest_codebook
from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID
from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    CodesDerived,
    CodesSupportingQuotes,
    CodingQueueEntry,
    DERIVATION_AGGREGATION,
    Quote,
    Segment,
)
from thematic_analysis_inc.db.research_context import (
    latest_research_context_version,
)


@dataclass
class AggregatorMergeInput:
    """One merged/retained code from the aggregator agent."""

    code: str
    description: str = ""
    rationale: str = ""
    quote_texts: list[str] = field(default_factory=list)
    source_codes: list[Code] = field(default_factory=list)


def next_segment_to_aggregate() -> Segment | None:
    """Next Segment whose every queue row is finished without error and
    that has no aggregator code yet. Returns None if none is ready."""
    with session() as s:
        # Need at least one real coder.
        from thematic_analysis_inc.db.models import Coder

        n_coders = int(
            s.exec(
                select(func.count())
                .select_from(Coder)
                .where(Coder.coder_id >= 1)
            ).one()
        )
        if n_coders == 0:
            return None
        # Subqueries.
        has_unfinished = (
            select(CodingQueueEntry.segment_id)
            .where(
                CodingQueueEntry.segment_id == Segment.segment_id,
                (CodingQueueEntry.finished_at.is_(None))  # type: ignore[union-attr]
                | (CodingQueueEntry.error.is_not(None)),  # type: ignore[union-attr]
            )
            .exists()
        )
        has_any_queue = (
            select(CodingQueueEntry.segment_id)
            .where(CodingQueueEntry.segment_id == Segment.segment_id)
            .exists()
        )
        has_agg = (
            select(Code.code_id)
            .where(
                Code.segment_id == Segment.segment_id,
                Code.coder_id == SYSTEM_AGGREGATOR_ID,
            )
            .exists()
        )
        has_coder_code = (
            select(Code.code_id)
            .where(
                Code.segment_id == Segment.segment_id,
                Code.coder_id >= 1,
            )
            .exists()
        )
        seg = s.exec(
            select(Segment)
            .where(has_any_queue, ~has_unfinished, ~has_agg, has_coder_code)
            .order_by(Segment.segment_id)
            .limit(1)
        ).first()
        if seg is not None:
            _ = seg.content  # eager-load
            s.expunge(seg)
        return seg


def segment_has_aggregator_code(segment_id: int) -> bool:
    with session() as s:
        row = s.exec(
            select(Code.code_id).where(
                Code.segment_id == segment_id,
                Code.coder_id == SYSTEM_AGGREGATOR_ID,
            ).limit(1)
        ).first()
        return row is not None


def record_aggregation_result(
    segment: Segment, merged: list[AggregatorMergeInput]
) -> list[Code]:
    """For each merged code: insert Quote rows, an aggregator Code row,
    quote links, and ``CodesDerived('A', ...)`` edges per source. Returns
    the new aggregator Codes (detached)."""
    cb = latest_codebook()
    if cb is None:
        raise RuntimeError("no codebook revision exists")
    rc_version = latest_research_context_version()
    out_ids: list[int] = []
    out_texts: list[tuple[str, str, str]] = []
    with session() as s:
        for inp in merged:
            agg_code = Code(
                segment_id=segment.segment_id,
                coder_id=SYSTEM_AGGREGATOR_ID,
                codebook_used_id=cb.version,
                research_context_used_id=rc_version,
                code=inp.code,
                description=inp.description or "",
                rationale=inp.rationale or "",
            )
            s.add(agg_code)
            s.commit()
            s.refresh(agg_code)
            cid = agg_code.code_id
            for text in inp.quote_texts or []:
                q = Quote(segment_id=segment.segment_id, text=text)
                s.add(q)
                s.commit()
                s.refresh(q)
                s.add(
                    CodesSupportingQuotes(code_id=cid, quote_id=q.quote_id)
                )
            for src in inp.source_codes or []:
                existing = s.get(CodesDerived, (cid, src.code_id))
                if existing is None:
                    s.add(
                        CodesDerived(
                            new_code_id=cid,
                            source_code_id=src.code_id,
                            derivation_type=DERIVATION_AGGREGATION,
                        )
                    )
            s.commit()
            out_ids.append(cid)
            out_texts.append(
                (inp.code, inp.description or "", inp.rationale or "")
            )
    # Build detached Code objects for the caller (no session attachment).
    return [
        Code(
            code_id=cid,
            segment_id=segment.segment_id,
            coder_id=SYSTEM_AGGREGATOR_ID,
            codebook_used_id=cb.version,
            research_context_used_id=rc_version,
            code=code,
            description=desc,
            rationale=rat,
        )
        for cid, (code, desc, rat) in zip(out_ids, out_texts)
    ]


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
    with session() as s:
        rows = list(
            s.exec(
                select(Code)
                .where(
                    Code.segment_id == segment_id,
                    Code.coder_id == SYSTEM_AGGREGATOR_ID,
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
