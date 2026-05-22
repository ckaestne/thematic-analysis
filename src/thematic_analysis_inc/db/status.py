"""Derived status counts for dashboards / status payloads."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlmodel import select

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    CodebookCode,
    Coder,
    CodesDerived,
    CodingQueueEntry,
    Segment,
    DERIVATION_REVIEW,
    SENTINEL_CODE_LABEL,
)


@dataclass
class StatusCounts:
    segments_total: int
    segments_by_status: dict[str, int]
    coders_total: int
    coding_queue_total: int
    coding_queue_by_status: dict[str, int]
    aggregator_codes_total: int
    aggregator_segments_total: int
    reviewer_codes_total: int
    review_decisions_by_kind: dict[str, int]
    codebook_version: int
    codebook_codes: int

    def format(self) -> str:
        def by_status(d: dict[str, int]) -> str:
            if not d:
                return "(none)"
            return " | ".join(f"{k}={v}" for k, v in sorted(d.items()))

        lines = [
            f"segments:         {self.segments_total} total | "
            f"{by_status(self.segments_by_status)}",
            f"coders:           {self.coders_total}",
            f"coding_queue:     {self.coding_queue_total} total | "
            f"{by_status(self.coding_queue_by_status)}",
            f"aggregator codes: {self.aggregator_codes_total} "
            f"(segments={self.aggregator_segments_total})",
            f"reviewer codes:   {self.reviewer_codes_total} | "
            f"{by_status(self.review_decisions_by_kind)}",
            f"codebook:         v={self.codebook_version} "
            f"codes={self.codebook_codes}",
        ]
        return "\n".join(lines)


def derive_segment_status(segment: Segment | int) -> str:
    seg_id = segment if isinstance(segment, int) else segment.segment_id
    with session() as s:
        qrows = list(
            s.exec(
                select(CodingQueueEntry).where(
                    CodingQueueEntry.segment_id == seg_id
                )
            ).all()
        )
        if not qrows:
            return "pending"
        if any(r.error is not None for r in qrows):
            return "failed"
        if any(
            r.finished_at is None and r.claimed_at is not None for r in qrows
        ):
            return "coding"
        if any(r.claimed_at is None for r in qrows):
            return "pending"
        has_agg = s.exec(
            select(Code.code_id).where(
                Code.segment_id == seg_id, Code.coder_id == 0
            ).limit(1)
        ).first()
        if has_agg is None:
            return "aggregating"
        outgoing_r = (
            select(CodesDerived.source_code_id)
            .where(
                CodesDerived.source_code_id == Code.code_id,
                CodesDerived.derivation_type == DERIVATION_REVIEW,
            )
            .exists()
        )
        remaining = int(
            s.exec(
                select(func.count())
                .select_from(Code)
                .where(
                    Code.segment_id == seg_id,
                    Code.coder_id == 0,
                    Code.code != SENTINEL_CODE_LABEL,
                    ~outgoing_r,
                )
            ).one()
        )
    return "reviewing" if remaining > 0 else "done"


def segments_by_derived_status() -> dict[str, int]:
    out: dict[str, int] = {}
    with session() as s:
        ids = list(s.exec(select(Segment.segment_id)).all())  # type: ignore[arg-type]
    for sid in ids:
        st = derive_segment_status(sid)
        out[st] = out.get(st, 0) + 1
    return out


def _coding_queue_by_status() -> dict[str, int]:
    out: dict[str, int] = {}
    with session() as s:
        rows = list(s.exec(select(CodingQueueEntry)).all())
    for r in rows:
        out[r.status] = out.get(r.status, 0) + 1
    return out


def status_counts() -> StatusCounts:
    with session() as s:
        seg_total = int(
            s.exec(select(func.count()).select_from(Segment)).one()
        )
        coders_total = int(
            s.exec(
                select(func.count())
                .select_from(Coder)
                .where(Coder.coder_id >= 1)
            ).one()
        )
        agg_codes_total = int(
            s.exec(
                select(func.count())
                .select_from(Code)
                .where(Code.coder_id == 0)
            ).one()
        )
        agg_segs_total = int(
            s.exec(
                select(func.count(func.distinct(Code.segment_id))).where(
                    Code.coder_id == 0
                )
            ).one()
        )
        rev_total = int(
            s.exec(
                select(func.count())
                .select_from(Code)
                .where(Code.coder_id == -1)
            ).one()
        )
        rev_rows = list(
            s.exec(
                select(CodesDerived.decision, func.count())
                .where(CodesDerived.derivation_type == DERIVATION_REVIEW)
                .group_by(CodesDerived.decision)
            ).all()
        )
        rev_by = {r[0]: int(r[1]) for r in rev_rows}
        cb_row = s.exec(
            select(Codebook).order_by(Codebook.version.desc()).limit(1)  # type: ignore[union-attr]
        ).first()
        cb_version = 0 if cb_row is None else int(cb_row.version)
        cb_codes = int(
            s.exec(
                select(func.count())
                .select_from(CodebookCode)
                .where(CodebookCode.codebook_version == cb_version)
            ).one()
        )

    cq_by = _coding_queue_by_status()
    return StatusCounts(
        segments_total=seg_total,
        segments_by_status=segments_by_derived_status(),
        coders_total=coders_total,
        coding_queue_total=sum(cq_by.values()),
        coding_queue_by_status=cq_by,
        aggregator_codes_total=agg_codes_total,
        aggregator_segments_total=agg_segs_total,
        reviewer_codes_total=rev_total,
        review_decisions_by_kind=rev_by,
        codebook_version=cb_version,
        codebook_codes=cb_codes,
    )


