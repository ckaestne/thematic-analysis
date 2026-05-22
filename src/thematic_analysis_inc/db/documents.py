"""Document, Segment, Quote helpers (SQLModel-backed)."""

from __future__ import annotations

from sqlalchemy import func, or_
from sqlmodel import select

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import Code, Document, Quote, Segment


def add_document(filename: str) -> Document:
    """Insert a Document. Returns the persisted Document (detached)."""
    with session() as s:
        d = Document(filename=filename)
        s.add(d)
        s.commit()
        s.refresh(d)
        s.expunge(d)
        return d


def find_document_by_filename(filename: str) -> Document | None:
    with session() as s:
        d = s.exec(
            select(Document)
            .where(Document.filename == filename)
            .order_by(Document.document_id.asc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        if d is not None:
            s.expunge(d)
        return d


def list_documents() -> list[Document]:
    """All documents, newest first. Callers can read ``doc.segments`` to count."""
    with session() as s:
        rows = list(
            s.exec(
                select(Document).order_by(
                    Document.document_id.desc()  # type: ignore[union-attr]
                )
            ).all()
        )
        # Force-load segments while session is open so callers can use
        # ``doc.segments`` afterwards.
        for d in rows:
            _ = d.segments  # noqa: B018
            s.expunge(d)
        return rows


def enqueue_segments(
    document: Document, segments: list[tuple[str | None, str, int, int, int]]
) -> list[Segment]:
    """Persist a batch of Segments under ``document``. Each tuple is
    ``(title, content, line_from, line_to, position)``."""
    with session() as s:
        out: list[Segment] = []
        for title, content, line_from, line_to, position in segments:
            seg = Segment(
                document_id=document.document_id,
                title=title,
                content=content,
                line_from=line_from,
                line_to=line_to,
                position=position,
            )
            s.add(seg)
            out.append(seg)
        s.commit()
        for seg in out:
            s.refresh(seg)
            s.expunge(seg)
        return out


def count_segments() -> int:
    """Total number of segments across all documents."""
    with session() as s:
        return int(s.exec(select(func.count()).select_from(Segment)).one())


def get_segment(segment_id: int) -> Segment | None:
    from sqlalchemy.orm import selectinload

    with session() as s:
        seg = s.exec(
            select(Segment)
            .where(Segment.segment_id == segment_id)
            .options(selectinload(Segment.quotes))  # type: ignore[arg-type]
        ).first()
        if seg is not None:
            s.expunge_all()
        return seg


def get_segment_with_codes(segment_id: int) -> Segment | None:
    """Like :func:`get_segment`, but also eager-loads
    ``segment.codes`` and each code's ``supporting_quotes`` so the
    aggregator/reviewer paths can traverse the whole graph after the
    session closes."""
    from sqlalchemy.orm import selectinload

    with session() as s:
        seg = s.exec(
            select(Segment)
            .where(Segment.segment_id == segment_id)
            .options(
                selectinload(Segment.quotes),  # type: ignore[arg-type]
                selectinload(Segment.codes).selectinload(  # type: ignore[arg-type]
                    Code.supporting_quotes  # type: ignore[arg-type]
                ),
            )
        ).first()
        if seg is not None:
            s.expunge_all()
        return seg


def list_segments(
    q: str | None = None, limit: int = 100, offset: int = 0
) -> tuple[int, list[Segment]]:
    with session() as s:
        stmt = select(Segment)
        count_stmt = select(func.count()).select_from(Segment)
        if q:
            like = f"%{q}%"
            cond = or_(
                Segment.content.like(like),  # type: ignore[attr-defined]
                func.cast(Segment.segment_id, type_=None).like(like),
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        total = int(s.exec(count_stmt).one())
        rows = list(
            s.exec(
                stmt.order_by(Segment.segment_id).limit(limit).offset(offset)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return total, rows


def add_quote(segment: Segment, text: str) -> Quote:
    """Persist a Quote on ``segment``, reusing an existing near-identical
    one if present (see ``create_quote``)."""
    from thematic_analysis_inc.db.models import create_quote

    with session() as s:
        seg = s.get(Segment, segment.segment_id)
        if seg is None:
            raise RuntimeError(f"segment {segment.segment_id} not found")
        q = create_quote(seg, text)
        if q.quote_id is not None:
            s.expunge(q)
            return q
        s.add(q)
        s.commit()
        s.refresh(q)
        s.expunge(q)
        return q


def link_code_quote(code: Code, quote: Quote) -> None:
    """Attach ``quote`` to ``code`` via the n:m link table. Idempotent."""
    from thematic_analysis_inc.db.models import CodesSupportingQuotes

    with session() as s:
        existing = s.get(
            CodesSupportingQuotes, (code.code_id, quote.quote_id)
        )
        if existing is None:
            s.add(
                CodesSupportingQuotes(
                    code_id=code.code_id, quote_id=quote.quote_id
                )
            )
            s.commit()
