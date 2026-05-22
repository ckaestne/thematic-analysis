"""Smoke tests for the SQLModel schema in `db/models.py`.

Verifies every table is created, every relationship navigates in both
directions, the CodingQueueEntry status property derives correctly, the
self-referential parent/children chain on Codebook works, and the CHECK
constraints on codes_derived reject invalid enum values.

Uses an in-memory SQLite engine — no on-disk state, no interaction with
the raw-SQL `db/` modules.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, delete, select

from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    CodebookCode,
    Coder,
    CodesDerived,
    CodingQueueEntry,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_MERGE_AND_RENAME,
    DERIVATION_AGGREGATION,
    DERIVATION_REVIEW,
    Document,
    Quote,
    ResearchContext,
    Segment,
)


EXPECTED_TABLES = {
    "codebook",
    "codebook_code",
    "coder",
    "code",
    "codes_derived",
    "codes_supporting_quotes",
    "coding_queue",
    "document",
    "quote",
    "research_context",
    "segment",
}


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _seed_world(session: Session) -> dict:
    """Build a minimal world that exercises every relationship and return
    the key objects for assertions."""
    session.add(Coder(coder_id=0, identity="aggregator"))
    session.add(Coder(coder_id=-1, identity="reviewer"))
    rc1 = ResearchContext(description="initial", coder_prompt="careful")
    session.add(rc1)
    session.commit()
    session.refresh(rc1)
    cb1 = Codebook(research_context_version=rc1.research_context_version)
    session.add(cb1)
    session.commit()
    session.refresh(cb1)

    alice = Coder(coder_id=1, identity="alice")
    session.add(alice)
    doc = Document(filename="d.md")
    session.add(doc)
    session.commit()
    session.refresh(doc)

    seg = Segment(
        document=doc, content="seg one",
        line_from=1, line_to=2, position=0,
    )
    session.add(seg)
    session.commit()
    session.refresh(seg)

    coder_code = Code(
        segment=seg, coder=alice, codebook_used=cb1,
        code="resistance", rationale="why",
    )
    session.add(coder_code)
    session.commit()
    session.refresh(coder_code)

    q = Quote(segment=seg, text="I refuse")
    agg = Code(
        segment=seg, coder_id=0, codebook_used=cb1,
        code="resistance",
    )
    agg.supporting_quotes.append(q)
    session.add(agg)
    session.commit()
    session.refresh(agg)
    session.add(
        CodesDerived(
            new_code_id=agg.code_id, source_code_id=coder_code.code_id,
            derivation_type=DERIVATION_AGGREGATION,
        )
    )
    session.commit()

    rev = Code(
        segment=seg, coder_id=-1, codebook_used=cb1,
        code="resistance",
        description="opposing change",
    )
    session.add(rev)
    session.commit()
    session.refresh(rev)
    session.add(
        CodebookCode(codebook_version=cb1.version, code_id=rev.code_id)
    )
    session.add(
        CodesDerived(
            new_code_id=rev.code_id, source_code_id=agg.code_id,
            derivation_type=DERIVATION_REVIEW,
            decision=DECISION_ADD, rationale="add it",
        )
    )
    session.commit()

    session.add(
        CodingQueueEntry(
            segment_id=seg.segment_id, coder_id=alice.coder_id,
            codebook_used_id=cb1.version,
        )
    )
    session.commit()

    return {
        "cb1": cb1, "rc1": rc1, "alice": alice, "doc": doc, "seg": seg,
        "coder_code": coder_code, "agg": agg, "rev": rev,
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_all_tables_are_created(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables, EXPECTED_TABLES - tables


def test_document_has_no_content_column(engine) -> None:
    cols = {c["name"] for c in inspect(engine).get_columns("document")}
    assert cols == {"document_id", "filename", "created_at"}


def test_coder_has_no_name_column(engine) -> None:
    cols = {c["name"] for c in inspect(engine).get_columns("coder")}
    assert "name" not in cols
    assert cols == {"coder_id", "identity", "created_at"}


def test_codebook_pins_research_context(engine) -> None:
    """Codebook stores the research-context version it was authored against."""
    cols = {c["name"] for c in inspect(engine).get_columns("codebook")}
    assert cols == {
        "version", "parent_version", "research_context_version", "created_at",
    }


def test_coder_id_is_not_autoincrement(session: Session) -> None:
    session.add(Coder(coder_id=0, identity="aggregator"))
    session.add(Coder(coder_id=-1, identity="reviewer"))
    session.commit()
    rows = session.exec(select(Coder).order_by(Coder.coder_id)).all()
    assert [c.coder_id for c in rows] == [-1, 0]


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


def test_code_navigates_to_segment_coder_codebook_and_research_context(
    session: Session,
) -> None:
    world = _seed_world(session)
    code = session.exec(
        select(Code).where(Code.code_id == world["coder_code"].code_id)
    ).one()
    assert code.segment.content == "seg one"
    assert code.coder.identity == "alice"
    assert code.codebook_used.version == world["cb1"].version
    # Research context is reached via the codebook revision.
    assert code.codebook_used.research_context.description == "initial"


def test_document_segments_back_ref(session: Session) -> None:
    world = _seed_world(session)
    doc = session.exec(select(Document)).one()
    assert [s.content for s in doc.segments] == ["seg one"]
    assert doc.segments[0].document is doc


def test_aggregator_code_exposes_quotes_and_derivation_edges(
    session: Session,
) -> None:
    world = _seed_world(session)
    agg = session.exec(
        select(Code).where(Code.code_id == world["agg"].code_id)
    ).one()

    assert [q.text for q in agg.supporting_quotes] == ["I refuse"]
    assert [d.derivation_type for d in agg.derivation_sources] == [
        DERIVATION_AGGREGATION
    ]
    assert agg.derivation_sources[0].source_code.code == "resistance"


def test_codebook_lists_member_codes(session: Session) -> None:
    world = _seed_world(session)
    cb = session.exec(
        select(Codebook).where(Codebook.version == world["cb1"].version)
    ).one()
    assert [c.code for c in cb.codes] == ["resistance"]


def test_coding_queue_status_property(session: Session) -> None:
    world = _seed_world(session)
    q = session.exec(select(CodingQueueEntry)).one()
    assert q.status == "pending"
    assert q.segment.segment_id == world["seg"].segment_id
    assert q.coder.identity == "alice"

    q.claimed_at = datetime.now(timezone.utc)
    assert q.status == "running"
    q.finished_at = datetime.now(timezone.utc)
    assert q.status == "done"
    q.error = "boom"
    assert q.status == "failed"


def test_codebook_parent_chain(session: Session) -> None:
    rc = ResearchContext(description="x")
    session.add(rc)
    session.commit()
    session.refresh(rc)
    cb1 = Codebook(research_context_version=rc.research_context_version)
    session.add(cb1)
    session.commit()
    session.refresh(cb1)
    cb2 = Codebook(
        parent_version=cb1.version,
        research_context_version=rc.research_context_version,
    )
    session.add(cb2)
    session.commit()
    session.refresh(cb2)
    assert cb2.parent is not None
    assert cb2.parent.version == cb1.version
    assert cb1.parent is None
    # children walks forward
    assert [c.version for c in cb1.children] == [cb2.version]


def test_quote_back_ref_to_codes(session: Session) -> None:
    world = _seed_world(session)
    q = session.exec(select(Quote)).one()
    assert q.segment.segment_id == world["seg"].segment_id
    assert [c.code for c in q.codes] == ["resistance"]


# ---------------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------------


def test_derivation_type_check_rejects_unknown(session: Session) -> None:
    _seed_world(session)
    cid = session.exec(select(Code.code_id)).first()
    with pytest.raises(IntegrityError):
        session.add(
            CodesDerived(
                new_code_id=cid, source_code_id=cid, derivation_type="X"
            )
        )
        session.commit()


def test_decision_check_rejects_unknown(session: Session) -> None:
    _seed_world(session)
    cid = session.exec(select(Code.code_id)).first()
    with pytest.raises(IntegrityError):
        session.add(
            CodesDerived(
                new_code_id=cid, source_code_id=cid,
                derivation_type="R", decision="Z",
            )
        )
        session.commit()


def test_all_three_decisions_accepted(session: Session) -> None:
    _seed_world(session)
    rev = session.exec(select(Code).where(Code.coder_id == -1)).first()
    agg = session.exec(select(Code).where(Code.coder_id == 0)).first()
    session.exec(
        delete(CodesDerived).where(
            CodesDerived.new_code_id == rev.code_id,
            CodesDerived.source_code_id == agg.code_id,
        )
    )
    session.commit()
    for decision in (DECISION_ADD, DECISION_MERGE, DECISION_MERGE_AND_RENAME):
        session.add(
            CodesDerived(
                new_code_id=rev.code_id, source_code_id=agg.code_id,
                derivation_type=DERIVATION_REVIEW, decision=decision,
            )
        )
        session.commit()
        session.exec(
            delete(CodesDerived).where(
                CodesDerived.new_code_id == rev.code_id,
                CodesDerived.source_code_id == agg.code_id,
            )
        )
        session.commit()
