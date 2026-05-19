"""Smoke tests for the SQLModel schema in `db/models.py`.

These verify that every table is created, every relationship navigates,
and the check constraints reject invalid enum values. They use an
in-memory SQLite engine — no on-disk state, no interaction with the
raw-SQL `db/` modules (yet).
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, delete, select

from thematic_analysis_inc.db.models import (
    Code,
    Coder,
    CodebookMembership,
    CodebookVersion,
    CodesDerived,
    CodingQueueEntry,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_UPDATE,
    DERIVATION_AGGREGATION,
    DERIVATION_REVIEW,
    Document,
    Quote,
    ResearchContext,
    Segment,
    ThemeAggregation,
    ThemeCoder,
    ThemeCoderRun,
)


EXPECTED_TABLES = {
    "codebook",
    "codebook_versions",
    "coders",
    "codes",
    "codes_derived",
    "codes_supporting_quotes",
    "coding_queue",
    "documents",
    "quotes",
    "research_context",
    "segments",
    "theme_aggregation_inputs",
    "theme_aggregations",
    "theme_coder_runs",
    "theme_coders",
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
    """Build a minimal world that covers every relationship and return
    the key objects for assertions."""
    session.add(Coder(coder_id=0, identity="aggregator"))
    session.add(Coder(coder_id=-1, identity="reviewer"))
    cv1 = CodebookVersion(created_by="init")
    session.add(cv1)
    rc1 = ResearchContext(description="initial", coder_prompt="careful")
    session.add(rc1)
    session.commit()
    session.refresh(cv1)
    session.refresh(rc1)

    alice = Coder(coder_id=1, identity="alice")
    session.add(alice)
    doc = Document(filename="d.md", content=b"hello")
    session.add(doc)
    session.commit()
    session.refresh(doc)

    seg = Segment(document=doc, content="seg one", line_from=1, line_to=2)
    session.add(seg)
    session.commit()
    session.refresh(seg)

    coder_code = Code(
        segment=seg, coder=alice, codebook_version=cv1,
        research_context=rc1, code="resistance", rationale="why",
    )
    session.add(coder_code)
    session.commit()
    session.refresh(coder_code)

    q = Quote(segment=seg, text="I refuse")
    agg = Code(
        segment=seg, coder_id=0, codebook_version=cv1,
        research_context=rc1, code="resistance",
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
        coder_id=-1, codebook_version=cv1, research_context=rc1,
        code="resistance", description="opposing change",
    )
    session.add(rev)
    session.commit()
    session.refresh(rev)
    session.add(
        CodebookMembership(codebook_version_id=cv1.version, code_id=rev.code_id)
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
            codebook_version_id=cv1.version,
            research_context_version_id=rc1.research_context_version,
        )
    )
    session.commit()

    tc = ThemeCoder(theme_coder_id="t1", identity="critic")
    session.add(tc)
    session.commit()
    tcr = ThemeCoderRun(
        theme_coder_id="t1",
        codebook_version_id=cv1.version,
        research_context_version_id=rc1.research_context_version,
    )
    session.add(tcr)
    session.commit()
    ta = ThemeAggregation(
        codebook_version_id=cv1.version,
        research_context_version_id=rc1.research_context_version,
    )
    session.add(ta)
    session.commit()
    ta.input_runs.append(tcr)
    session.commit()

    return {
        "cv1": cv1, "rc1": rc1, "alice": alice, "doc": doc, "seg": seg,
        "coder_code": coder_code, "agg": agg, "rev": rev, "tcr": tcr,
        "ta": ta,
    }


# ---------------------------------------------------------------------------
# Schema basics
# ---------------------------------------------------------------------------


def test_all_tables_are_created(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables, EXPECTED_TABLES - tables


def test_coder_id_is_not_autoincrement(session: Session) -> None:
    """System coders need explicit ids 0 and -1; explicit-id inserts must
    persist as-is."""
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
    assert code.codebook_version.version == world["cv1"].version
    assert code.research_context.description == "initial"


def test_aggregator_code_exposes_quotes_and_provenance_edges(
    session: Session,
) -> None:
    world = _seed_world(session)
    agg = session.exec(
        select(Code).where(Code.code_id == world["agg"].code_id)
    ).one()

    assert [q.text for q in agg.supporting_quotes] == ["I refuse"]
    # derivation_sources: rows where this code is the result
    assert [d.derivation_type for d in agg.derivation_sources] == [
        DERIVATION_AGGREGATION
    ]
    assert agg.derivation_sources[0].source_code.code == "resistance"
    # derivation_targets: rows where this code is the source
    assert [
        (d.derivation_type, d.decision) for d in agg.derivation_targets
    ] == [(DERIVATION_REVIEW, DECISION_ADD)]
    assert agg.derivation_targets[0].new_code.code == "resistance"


def test_codebook_version_lists_member_codes(session: Session) -> None:
    world = _seed_world(session)
    cv = session.exec(
        select(CodebookVersion).where(
            CodebookVersion.version == world["cv1"].version
        )
    ).one()
    assert [c.code for c in cv.member_codes] == ["resistance"]


def test_coding_queue_status_property(session: Session) -> None:
    from datetime import datetime, timezone

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


def test_codebook_version_parent_chain(session: Session) -> None:
    v1 = CodebookVersion(created_by="init")
    session.add(v1)
    session.commit()
    session.refresh(v1)
    v2 = CodebookVersion(parent_version_id=v1.version, created_by="reviewer")
    session.add(v2)
    session.commit()
    session.refresh(v2)
    assert v2.parent is not None
    assert v2.parent.version == v1.version
    assert v1.parent is None


def test_theme_aggregation_navigates_to_input_runs_and_codebook(
    session: Session,
) -> None:
    world = _seed_world(session)
    ta = session.exec(select(ThemeAggregation)).one()
    assert len(ta.input_runs) == 1
    assert ta.input_runs[0].theme_coder.identity == "critic"
    assert ta.codebook_version.version == world["cv1"].version
    assert ta.research_context.description == "initial"


def test_quote_belongs_to_segment_and_back_to_codes(session: Session) -> None:
    world = _seed_world(session)
    q = session.exec(select(Quote)).one()
    assert q.segment.segment_id == world["seg"].segment_id
    assert [c.code for c in q.codes] == ["resistance"]


def test_research_context_lists_dependent_rows(session: Session) -> None:
    world = _seed_world(session)
    rc = session.exec(
        select(ResearchContext).where(
            ResearchContext.research_context_version
            == world["rc1"].research_context_version
        )
    ).one()
    # 3 codes (coder + aggregator + reviewer), 1 queue, 1 theme run,
    # 1 theme aggregation
    assert len(rc.codes) == 3
    assert len(rc.coding_queue_entries) == 1
    assert len(rc.theme_coder_runs) == 1
    assert len(rc.theme_aggregations) == 1


# ---------------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------------


def test_derivation_type_check_constraint_rejects_unknown(
    session: Session,
) -> None:
    """`derivation_type` must be 'A' or 'R'."""
    _seed_world(session)
    # Use any two existing code_ids
    cid = session.exec(select(Code.code_id)).first()
    with pytest.raises(IntegrityError):
        session.add(
            CodesDerived(
                new_code_id=cid, source_code_id=cid, derivation_type="X"
            )
        )
        session.commit()


def test_decision_check_constraint_rejects_unknown(session: Session) -> None:
    """`decision` must be NULL or one of 'A'/'M'/'U'."""
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


def test_decision_constants_match_check_constraint(session: Session) -> None:
    """All three valid decision values are accepted."""
    _seed_world(session)
    rev = session.exec(
        select(Code).where(Code.coder_id == -1)
    ).first()
    agg = session.exec(
        select(Code).where(Code.coder_id == 0)
    ).first()
    # Replace the existing R edge first.
    session.exec(
        delete(CodesDerived).where(
            CodesDerived.new_code_id == rev.code_id,
            CodesDerived.source_code_id == agg.code_id,
        )
    )
    session.commit()
    for decision in (DECISION_ADD, DECISION_MERGE, DECISION_UPDATE):
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
