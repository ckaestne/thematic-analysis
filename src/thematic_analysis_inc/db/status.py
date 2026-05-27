"""Derived status counts for dashboards / status payloads."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import case, func
from sqlmodel import select

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    CodebookCode,
    Coder,
    CodesDerived,
    CodingQueueEntry,
    Document,
    Quote,
    Segment,
    Theme,
    DERIVATION_REVIEW,
    SENTINEL_CODE_LABEL,
    SENTINEL_RUNNING_THEME_TITLE,
    SENTINEL_THEME_TITLE,
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
    documents_total: int = 0
    codes_total: int = 0
    quotes_total: int = 0
    themes_total: int = 0
    documents_by_coding_status: dict[str, int] = field(default_factory=dict)
    reviews_pending: int = 0
    reviews_completed_since_codebook: int = 0

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
    """Bucket every Segment into the same status categories as
    :func:`derive_segment_status`, in one SQL round trip.

    Polled every few seconds by the dashboard, so per-segment iteration
    in Python is not affordable. The query builds a per-segment row of
    queue/aggregator/reviewer counts (LEFT JOIN to ``coding_queue`` plus
    two correlated scalar subqueries on ``code``) and applies the same
    bucketing rules as :func:`derive_segment_status` via a ``CASE``,
    then groups on that expression to return one count per status.
    """
    outgoing_r = (
        select(CodesDerived.source_code_id)
        .where(
            CodesDerived.source_code_id == Code.code_id,
            CodesDerived.derivation_type == DERIVATION_REVIEW,
        )
        .exists()
    )
    n_agg = (
        select(func.count())
        .select_from(Code)
        .where(
            Code.segment_id == Segment.segment_id,
            Code.coder_id == 0,
        )
        .correlate(Segment)
        .scalar_subquery()
    )
    n_unreviewed = (
        select(func.count())
        .select_from(Code)
        .where(
            Code.segment_id == Segment.segment_id,
            Code.coder_id == 0,
            Code.code != SENTINEL_CODE_LABEL,
            ~outgoing_r,
        )
        .correlate(Segment)
        .scalar_subquery()
    )

    n_queue = func.count(CodingQueueEntry.segment_id)
    n_errored = func.coalesce(
        func.sum(
            case((CodingQueueEntry.error.is_not(None), 1), else_=0)  # type: ignore[union-attr]
        ),
        0,
    )
    n_in_flight = func.coalesce(
        func.sum(
            case(
                (
                    (CodingQueueEntry.claimed_at.is_not(None))  # type: ignore[union-attr]
                    & (CodingQueueEntry.finished_at.is_(None)),  # type: ignore[union-attr]
                    1,
                ),
                else_=0,
            )
        ),
        0,
    )
    n_unclaimed = func.coalesce(
        func.sum(
            case((CodingQueueEntry.claimed_at.is_(None), 1), else_=0)  # type: ignore[union-attr]
        ),
        0,
    )

    per_seg = (
        select(
            Segment.segment_id.label("sid"),
            n_queue.label("n_queue"),
            n_errored.label("n_errored"),
            n_in_flight.label("n_in_flight"),
            n_unclaimed.label("n_unclaimed"),
            n_agg.label("n_agg"),
            n_unreviewed.label("n_unreviewed"),
        )
        .select_from(Segment)
        .outerjoin(
            CodingQueueEntry,
            CodingQueueEntry.segment_id == Segment.segment_id,
        )
        .group_by(Segment.segment_id)
        .subquery()
    )

    status_expr = case(
        (per_seg.c.n_queue == 0, "pending"),
        (per_seg.c.n_errored > 0, "failed"),
        (per_seg.c.n_in_flight > 0, "coding"),
        (per_seg.c.n_unclaimed > 0, "pending"),
        (per_seg.c.n_agg == 0, "aggregating"),
        (per_seg.c.n_unreviewed > 0, "reviewing"),
        else_="done",
    )

    with session() as s:
        rows = list(
            s.exec(
                select(status_expr.label("status"), func.count())
                .select_from(per_seg)
                .group_by(status_expr)
            ).all()
        )

    out: dict[str, int] = {}
    for status_name, n in rows:
        key = str(status_name)
        out[key] = out.get(key, 0) + int(n)
    return out


def _coding_queue_by_status() -> dict[str, int]:
    """Counts per ``CodingQueueEntry.status`` bucket, computed in SQL.

    Mirrors the ``status`` property on the model: ``failed`` if
    ``error`` is set, else ``done`` / ``running`` / ``pending`` based on
    ``finished_at`` / ``claimed_at``.
    """
    out: dict[str, int] = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    with session() as s:
        rows = list(
            s.exec(
                select(
                    case(
                        (CodingQueueEntry.error.is_not(None), "failed"),  # type: ignore[union-attr]
                        (CodingQueueEntry.finished_at.is_not(None), "done"),  # type: ignore[union-attr]
                        (CodingQueueEntry.claimed_at.is_not(None), "running"),  # type: ignore[union-attr]
                        else_="pending",
                    ).label("status"),
                    func.count(),
                ).group_by("status")
            ).all()
        )
    for status_name, n in rows:
        out[str(status_name)] = int(n)
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
    docs_total, docs_by_status = _documents_by_coding_status()
    codes_total, quotes_total = _codes_and_quotes_totals()
    themes_total = _themes_total()
    reviews_pending, reviews_completed = _codebook_review_progress(cb_version)
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
        documents_total=docs_total,
        codes_total=codes_total,
        quotes_total=quotes_total,
        themes_total=themes_total,
        documents_by_coding_status=docs_by_status,
        reviews_pending=reviews_pending,
        reviews_completed_since_codebook=reviews_completed,
    )


def _codes_and_quotes_totals() -> tuple[int, int]:
    """Total real (non-sentinel) Code rows and total Quote rows."""
    with session() as s:
        codes_total = int(
            s.exec(
                select(func.count())
                .select_from(Code)
                .where(Code.code != SENTINEL_CODE_LABEL)
            ).one()
        )
        quotes_total = int(
            s.exec(select(func.count()).select_from(Quote)).one()
        )
    return codes_total, quotes_total


def _themes_total() -> int:
    """Non-deleted, non-sentinel themes (the same definition the Themes
    page uses)."""
    with session() as s:
        return int(
            s.exec(
                select(func.count())
                .select_from(Theme)
                .where(
                    Theme.deleted == False,  # noqa: E712
                    Theme.title.not_in(  # type: ignore[union-attr]
                        (SENTINEL_THEME_TITLE, SENTINEL_RUNNING_THEME_TITLE)
                    ),
                )
            ).one()
        )


def _documents_by_coding_status() -> tuple[int, dict[str, int]]:
    """Bucket every Document into fully_coded / partially_coded / not_coded.

    - **fully_coded**: every segment has an aggregator code row
      (``coder_id == 0``, sentinel or real).
    - **partially_coded**: at least one segment has any code (coder,
      aggregator, or reviewer) but not all segments are fully coded.
    - **not_coded**: no segment has any code.

    Returns ``(docs_total, {bucket: count})``.
    """
    with session() as s:
        seg_rows = list(
            s.exec(
                select(Segment.document_id, Segment.segment_id)
            ).all()
        )
        agg_seg_ids = {
            int(sid)
            for sid in s.exec(
                select(Code.segment_id).where(Code.coder_id == 0).distinct()
            ).all()
        }
        any_coded_seg_ids = {
            int(sid)
            for sid in s.exec(
                select(Code.segment_id).distinct()
            ).all()
        }
        docs_total = int(
            s.exec(select(func.count()).select_from(Document)).one()
        )

    by_doc_total: dict[int, int] = {}
    by_doc_agg: dict[int, int] = {}
    by_doc_any: dict[int, int] = {}
    for doc_id, seg_id in seg_rows:
        by_doc_total[doc_id] = by_doc_total.get(doc_id, 0) + 1
        if seg_id in agg_seg_ids:
            by_doc_agg[doc_id] = by_doc_agg.get(doc_id, 0) + 1
        if seg_id in any_coded_seg_ids:
            by_doc_any[doc_id] = by_doc_any.get(doc_id, 0) + 1

    out = {"fully_coded": 0, "partially_coded": 0, "not_coded": 0}
    seen_doc_ids = set(by_doc_total.keys())
    for did, total in by_doc_total.items():
        n_agg = by_doc_agg.get(did, 0)
        n_any = by_doc_any.get(did, 0)
        if total > 0 and n_agg == total:
            out["fully_coded"] += 1
        elif n_any > 0:
            out["partially_coded"] += 1
        else:
            out["not_coded"] += 1
    # Documents with no segments yet — count them as not_coded.
    out["not_coded"] += max(0, docs_total - len(seen_doc_ids))
    return docs_total, out


def _codebook_review_progress(latest_cb_version: int) -> tuple[int, int]:
    """``(pending, completed_since_latest_codebook)`` for the codebook
    progress bar.

    *pending* — aggregator codes (``coder_id=0``, non-sentinel) without
    an outgoing 'R' edge: these still need to be reviewed.
    *completed_since_latest_codebook* — reviewer codes (``coder_id=-1``,
    non-sentinel) authored against the latest codebook revision: a
    review has been produced but a newer codebook revision that absorbs
    it has not yet been materialized.
    """
    with session() as s:
        outgoing_r = (
            select(CodesDerived.source_code_id)
            .where(
                CodesDerived.source_code_id == Code.code_id,
                CodesDerived.derivation_type == DERIVATION_REVIEW,
            )
            .exists()
        )
        pending = int(
            s.exec(
                select(func.count())
                .select_from(Code)
                .where(
                    Code.coder_id == 0,
                    Code.code != SENTINEL_CODE_LABEL,
                    ~outgoing_r,
                )
            ).one()
        )
        if latest_cb_version <= 0:
            return pending, 0
        completed = int(
            s.exec(
                select(func.count())
                .select_from(Code)
                .where(
                    Code.coder_id == -1,
                    Code.code != SENTINEL_CODE_LABEL,
                    Code.codebook_used_id == latest_cb_version,
                )
            ).one()
        )
    return pending, completed


