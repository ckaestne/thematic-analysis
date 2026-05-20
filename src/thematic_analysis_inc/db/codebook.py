"""Codebook revisions + membership + JSON snapshot serializer."""

from __future__ import annotations

import json

from sqlmodel import select

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    CodebookCode,
    Quote,
)


def insert_codebook_version(
    parent: Codebook | None = None,
    research_context_version: int | None = None,
) -> Codebook:
    """Insert a new Codebook revision; returns the persisted row.

    Every codebook revision pins exactly one research-context revision.
    ``research_context_version`` defaults to the latest revision in the
    table (callers that drive a review will normally let it default,
    since reviews don't change the research context). Raises if no
    research context exists yet.
    """
    if research_context_version is None:
        from thematic_analysis_inc.db.research_context import (
            latest_research_context_version,
        )

        research_context_version = latest_research_context_version()
    if research_context_version is None:
        raise RuntimeError(
            "no research-context revision exists; set one before "
            "creating a codebook revision"
        )
    with session() as s:
        cb = Codebook(
            parent_version=parent.version if parent is not None else None,
            research_context_version=research_context_version,
        )
        s.add(cb)
        s.commit()
        s.refresh(cb)
        s.expunge(cb)
        return cb


def latest_codebook() -> Codebook | None:
    with session() as s:
        cb = s.exec(
            select(Codebook)
            .order_by(Codebook.version.desc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        if cb is not None:
            s.expunge(cb)
        return cb


def get_codebook(version: int) -> Codebook | None:
    with session() as s:
        cb = s.get(Codebook, version)
        if cb is not None:
            s.expunge(cb)
        return cb


def get_codebook_with_codes_and_research_context(
    version: int,
) -> Codebook | None:
    """Like :func:`get_codebook`, but also eager-loads ``codebook.codes``
    and ``codebook.research_context`` so agents bound to the returned
    (detached) instance can traverse them without a live session."""
    from sqlalchemy.orm import selectinload

    with session() as s:
        cb = s.exec(
            select(Codebook)
            .where(Codebook.version == version)
            .options(
                selectinload(Codebook.codes),  # type: ignore[arg-type]
                selectinload(Codebook.research_context),  # type: ignore[arg-type]
            )
        ).first()
        if cb is None:
            return None
        _ = list(cb.codes)
        _ = cb.research_context
        s.expunge_all()
        return cb


def list_codebooks() -> list[Codebook]:
    with session() as s:
        rows = list(
            s.exec(select(Codebook).order_by(Codebook.version.asc())).all()  # type: ignore[union-attr]
        )
        for r in rows:
            s.expunge(r)
        return rows


def codebook_to_json_for_version(version: int) -> str:
    """Serialize codes-in-version as the legacy ``{"codes": [...]}`` shape
    that ``thematic_analysis.codebook.Codebook.from_json`` consumes."""
    with session() as s:
        codes = list(
            s.exec(
                select(Code)
                .join(CodebookCode, CodebookCode.code_id == Code.code_id)
                .where(CodebookCode.codebook_version == version)
                .order_by(Code.code_id)
            ).all()
        )
        out_codes: list[dict] = []
        for c in codes:
            quotes_payload = [
                {"quote_id": str(q.quote_id), "text": q.text}
                for q in sorted(
                    c.supporting_quotes, key=lambda x: x.quote_id
                )
            ]
            out_codes.append({"code": c.code, "quotes": quotes_payload})
        return json.dumps({"codes": out_codes}, indent=2)


def add_code_to_codebook(codebook: Codebook, code: Code) -> None:
    """Membership: place ``code`` into ``codebook`` (idempotent)."""
    with session() as s:
        existing = s.get(CodebookCode, (codebook.version, code.code_id))
        if existing is None:
            s.add(
                CodebookCode(
                    codebook_version=codebook.version, code_id=code.code_id
                )
            )
            s.commit()


def copy_codebook_membership(
    *,
    from_codebook: Codebook,
    to_codebook: Codebook,
    drop_code: Code | None = None,
    add_code: Code | None = None,
) -> None:
    """Copy membership from one Codebook to another.

    ``drop_code``: omit this code from the copy (used for UPDATE).
    ``add_code``: also include this code (used for ADD / UPDATE).
    """
    drop_id = drop_code.code_id if drop_code is not None else None
    add_id = add_code.code_id if add_code is not None else None
    with session() as s:
        rows = list(
            s.exec(
                select(CodebookCode).where(
                    CodebookCode.codebook_version == from_codebook.version
                )
            ).all()
        )
        for r in rows:
            if drop_id is not None and r.code_id == drop_id:
                continue
            existing = s.get(
                CodebookCode, (to_codebook.version, r.code_id)
            )
            if existing is None:
                s.add(
                    CodebookCode(
                        codebook_version=to_codebook.version,
                        code_id=r.code_id,
                    )
                )
        if add_id is not None:
            existing = s.get(CodebookCode, (to_codebook.version, add_id))
            if existing is None:
                s.add(
                    CodebookCode(
                        codebook_version=to_codebook.version, code_id=add_id
                    )
                )
        s.commit()
