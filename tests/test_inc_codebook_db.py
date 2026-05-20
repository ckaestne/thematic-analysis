"""Tests for the codebook-as-JSON view across versions.

These tests exercise the data-access layer in `thematic_analysis_inc.db`
end-to-end: they build codes / quotes / codebook versions through the
public API (`save_aggregator_codes`, `save_reviewer_decision`,
`materialize_codebook_revision`, `add_quote`, `link_code_quote`, …) and
verify what `_get_codebook_codes` and `codebook_to_json_for_version`
return for each version.

Focus: collecting all the data needed to print a codebook (codes,
descriptions, rationales, supporting quotes) and confirming that
older codebook versions stay stable when newer ones are added.
"""

from __future__ import annotations

import json
from pathlib import Path

from thematic_analysis_inc import db
from thematic_analysis_inc.db.aggregation import save_aggregator_codes
from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID, SYSTEM_REVIEWER_ID
from thematic_analysis_inc.db.codebook import (
    live_codes_for_batch,
    materialize_codebook_revision,
)
from thematic_analysis_inc.db.models import (
    Code,
    CodesDerived,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_UPDATE,
    DERIVATION_REVIEW,
)
from thematic_analysis_inc.db.review import (
    next_aggregated_code_to_review,
    save_reviewer_decision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init(tmp_path: Path):
    return db.init_db(tmp_path / "x.sqlite")


def _get_codebook_codes(version: int):
    """Return entries with .code, .code_id, .description, .rationale, .quotes."""
    from dataclasses import dataclass
    from thematic_analysis_inc.db.connection import session
    from thematic_analysis_inc.db.models import (
        Code,
        CodebookCode,
        Quote,
        CodesSupportingQuotes,
    )
    from sqlmodel import select

    @dataclass
    class _E:
        code_id: int
        code: str
        description: str
        rationale: str
        quotes: list[dict]

    out = []
    with session() as s:
        codes = list(
            s.exec(
                select(Code)
                .join(CodebookCode, CodebookCode.code_id == Code.code_id)
                .where(CodebookCode.codebook_version == version)
                .order_by(Code.code_id)
            ).all()
        )
        for c in codes:
            qrows = list(
                s.exec(
                    select(Quote)
                    .join(
                        CodesSupportingQuotes,
                        CodesSupportingQuotes.quote_id == Quote.quote_id,
                    )
                    .where(CodesSupportingQuotes.code_id == c.code_id)
                    .order_by(Quote.quote_id)
                ).all()
            )
            out.append(
                _E(
                    code_id=c.code_id,
                    code=c.code,
                    description=c.description or "",
                    rationale=c.rationale or "",
                    quotes=[
                        {"quote_id": str(q.quote_id), "text": q.text}
                        for q in qrows
                    ],
                )
            )
    return out


def _seed_segment(conn, content: str = "seg content") -> int:
    doc = db.add_document("doc.md")
    segs = db.enqueue_segments(doc, [(None, content, 1, 5, 0)])
    return segs[-1].segment_id


def _segment_obj(segment_id: int):
    return db.get_segment(segment_id)


def _code_obj(code_id: int) -> Code:
    """Fetch a Code with supporting_quotes and derivation_sources eager-loaded."""
    from sqlalchemy.orm import selectinload
    from sqlmodel import select
    from thematic_analysis_inc.db.connection import session

    with session() as s:
        c = s.exec(
            select(Code)
            .where(Code.code_id == code_id)
            .options(
                selectinload(Code.supporting_quotes),
                selectinload(Code.derivation_sources).selectinload(
                    CodesDerived.source_code
                ),
            )
        ).first()
        if c is None:
            raise AssertionError(f"code {code_id} not found")
        _ = list(c.supporting_quotes)
        _ = list(c.derivation_sources)
        for e in c.derivation_sources:
            _ = e.source_code
        s.expunge_all()
        return c


def _add_agg_code(
    conn,
    *,
    segment_id: int,
    version: int,
    code: str,
    description: str = "",
    rationale: str = "",
    quote_texts: list[str] | None = None,
) -> int:
    seg = _segment_obj(segment_id)
    quotes = [db.add_quote(seg, t) for t in (quote_texts or [])]
    new_code = Code(
        segment_id=segment_id,
        coder_id=SYSTEM_AGGREGATOR_ID,
        codebook_used_id=version,
        code=code,
        description=description,
        rationale=rationale,
    )
    new_code.supporting_quotes = list(quotes)
    new_codes = save_aggregator_codes([new_code])
    assert len(new_codes) == 1
    return new_codes[0].code_id


def _do_review_decision(
    *,
    source_agg_code_id: int,
    parent_version: int,
    decision: str,
    new_code_text: str,
    rationale: str = "",
    target_code_id: int | None = None,
) -> int:
    """Persist one reviewer decision (no finalize). Returns new reviewer
    code_id."""
    agg = _code_obj(source_agg_code_id)
    target = _code_obj(target_code_id) if target_code_id is not None else None
    quotes = list(agg.supporting_quotes or [])
    if target is not None:
        # union of target's quotes and aggregator's quotes
        seen = {q.quote_id for q in quotes}
        for q in target.supporting_quotes or []:
            if q.quote_id not in seen:
                quotes.append(q)
                seen.add(q.quote_id)

    new_code = Code(
        segment_id=agg.segment_id,
        coder_id=SYSTEM_REVIEWER_ID,
        codebook_used_id=parent_version,
        code=new_code_text,
        description="",
        rationale=rationale,
        embedding=None,
    )
    new_code.supporting_quotes = quotes
    edges = [
        CodesDerived(
            source_code=agg,
            derivation_type=DERIVATION_REVIEW,
            decision=decision,
            rationale=rationale,
        )
    ]
    if target is not None:
        edges.append(
            CodesDerived(
                source_code=target,
                derivation_type=DERIVATION_REVIEW,
                decision=decision,
                rationale=rationale,
            )
        )
    new_code.derivation_sources = edges
    persisted = save_reviewer_decision(new_code)
    return persisted.code_id


def _finalize(parent_version: int) -> int | None:
    """Materialize a new codebook revision. Returns new version or None."""
    parent = db.get_codebook_with_codes_and_research_context(parent_version)
    assert parent is not None
    new_cb = materialize_codebook_revision(parent)
    return new_cb.version if new_cb is not None else None


def _review_add_and_finalize(
    conn,
    *,
    agg_code_id: int,
    code_text: str,
    parent_version: int,
    rationale: str = "",
) -> tuple[int, int]:
    """ADD-style review followed by finalize. Returns (new_version, new_code_id)."""
    new_code_id = _do_review_decision(
        source_agg_code_id=agg_code_id,
        parent_version=parent_version,
        decision=DECISION_ADD,
        new_code_text=code_text,
        rationale=rationale,
    )
    new_version = _finalize(parent_version)
    assert new_version is not None
    return new_version, new_code_id


# ---------------------------------------------------------------------------
# Empty codebook
# ---------------------------------------------------------------------------


def test_empty_codebook_v1_json_is_empty(tmp_path: Path) -> None:
    _init(tmp_path)
    assert _get_codebook_codes(1) == []
    snap = json.loads(db.codebook_to_json_for_version(1))
    assert snap == {"codes": []}


def test_unknown_version_returns_empty(tmp_path: Path) -> None:
    _init(tmp_path)
    assert _get_codebook_codes(42) == []
    assert json.loads(db.codebook_to_json_for_version(42)) == {"codes": []}


# ---------------------------------------------------------------------------
# Single ADD: one code shows up in the new version's JSON
# ---------------------------------------------------------------------------


def test_codebook_add_creates_new_version_with_one_code(
    tmp_path: Path,
) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn, "alpha")
    agg = _add_agg_code(
        conn,
        segment_id=seg,
        version=1,
        code="resistance",
        quote_texts=["I refuse to change"],
    )

    new_version, new_code_id = _review_add_and_finalize(
        conn,
        agg_code_id=agg,
        code_text="resistance",
        parent_version=1,
        rationale="reviewer wants this in the codebook",
    )
    assert new_version == 2

    # v1 still empty
    assert _get_codebook_codes(1) == []

    # v2 has exactly the new reviewer code with the aggregator's quote
    entries = _get_codebook_codes(2)
    assert len(entries) == 1
    e = entries[0]
    assert e.code_id == new_code_id
    assert e.code == "resistance"
    assert [q["text"] for q in e.quotes] == ["I refuse to change"]


def test_codebook_json_shape_matches_legacy_contract(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)
    agg = _add_agg_code(
        conn, segment_id=seg, version=1, code="c1", quote_texts=["q1"]
    )
    _review_add_and_finalize(
        conn, agg_code_id=agg, code_text="c1", parent_version=1
    )
    snap = json.loads(db.codebook_to_json_for_version(2))
    assert set(snap.keys()) == {"codes"}
    assert len(snap["codes"]) == 1
    entry = snap["codes"][0]
    assert set(entry.keys()) == {"code", "quotes"}
    assert entry["code"] == "c1"
    assert entry["quotes"] == [
        {"quote_id": entry["quotes"][0]["quote_id"], "text": "q1"}
    ]


# ---------------------------------------------------------------------------
# Sequential ADDs across versions
# ---------------------------------------------------------------------------


def test_two_adds_yield_growing_codebook(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)
    a1 = _add_agg_code(
        conn, segment_id=seg, version=1, code="A", quote_texts=["qa"]
    )
    v2, _ = _review_add_and_finalize(
        conn, agg_code_id=a1, code_text="A", parent_version=1
    )
    a2 = _add_agg_code(
        conn, segment_id=seg, version=v2, code="B", quote_texts=["qb"]
    )
    v3, _ = _review_add_and_finalize(
        conn, agg_code_id=a2, code_text="B", parent_version=v2
    )

    assert [e.code for e in _get_codebook_codes(1)] == []
    assert [e.code for e in _get_codebook_codes(v2)] == ["A"]
    assert [e.code for e in _get_codebook_codes(v3)] == ["A", "B"]

    snap_v2 = json.loads(db.codebook_to_json_for_version(v2))
    assert [c["code"] for c in snap_v2["codes"]] == ["A"]


# ---------------------------------------------------------------------------
# UPDATE: new revision drops the previous target, adds a new reviewer Code
# ---------------------------------------------------------------------------


def test_update_replaces_target_in_new_version_only(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    a1 = _add_agg_code(
        conn, segment_id=seg, version=1, code="raw", quote_texts=["q1"]
    )
    v2, target_id = _review_add_and_finalize(
        conn, agg_code_id=a1, code_text="raw", parent_version=1
    )

    a2 = _add_agg_code(
        conn,
        segment_id=seg,
        version=v2,
        code="refined-source",
        quote_texts=["q2"],
    )
    new_code_id = _do_review_decision(
        source_agg_code_id=a2,
        parent_version=v2,
        decision=DECISION_UPDATE,
        new_code_text="refined",
        rationale="better wording",
        target_code_id=target_id,
    )
    v3 = _finalize(v2)
    assert v3 is not None

    v2_codes = [e.code for e in _get_codebook_codes(v2)]
    v3_codes = [(e.code, e.code_id) for e in _get_codebook_codes(v3)]
    assert v2_codes == ["raw"]  # untouched
    assert v3_codes == [("refined", new_code_id)]  # replaced

    # The previous target Code row still exists in the `code` table
    # (we never mutated or deleted it); it just dropped from v3 membership.
    assert _code_obj(target_id).code == "raw"


# ---------------------------------------------------------------------------
# MERGE: new revision drops the previous target, adds a new reviewer Code
# carrying the target's label and the quote union
# ---------------------------------------------------------------------------


def test_merge_replaces_target_with_quote_union(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    a1 = _add_agg_code(
        conn, segment_id=seg, version=1, code="orig", quote_texts=["a"]
    )
    v2, target_id = _review_add_and_finalize(
        conn, agg_code_id=a1, code_text="orig", parent_version=1
    )

    a2 = _add_agg_code(
        conn, segment_id=seg, version=v2, code="dup-source", quote_texts=["b"]
    )
    new_code_id = _do_review_decision(
        source_agg_code_id=a2,
        parent_version=v2,
        decision=DECISION_MERGE,
        new_code_text="orig",
        rationale="duplicate",
        target_code_id=target_id,
    )
    v3 = _finalize(v2)
    assert v3 is not None

    v3_entries = _get_codebook_codes(v3)
    assert [(e.code, e.code_id) for e in v3_entries] == [
        ("orig", new_code_id)
    ]
    # Quote union — both 'a' (from target) and 'b' (from aggregator) appear.
    assert [q["text"] for q in v3_entries[0].quotes] == ["a", "b"]

    # Provenance: the new reviewer Code has two 'R' edges, one to the
    # aggregator, one to the previous target reviewer code.
    new_code = _code_obj(new_code_id)
    edges = list(new_code.derivation_sources or [])
    assert len(edges) == 2
    assert all(e.derivation_type == DERIVATION_REVIEW for e in edges)
    source_ids = {e.source_code.code_id for e in edges if e.source_code is not None}
    assert source_ids == {a2, target_id}


# ---------------------------------------------------------------------------
# A no-decision finalize returns None
# ---------------------------------------------------------------------------


def test_finalize_no_decisions_returns_none(tmp_path: Path) -> None:
    _init(tmp_path)
    parent = db.latest_codebook()
    assert parent is not None
    assert _finalize(parent.version) is None


# ---------------------------------------------------------------------------
# live_codes_for_batch: reflects ADD/MERGE/UPDATE applied so far in batch
# ---------------------------------------------------------------------------


def test_live_codes_for_batch_reflects_in_progress_adds(
    tmp_path: Path,
) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    # First, finalize an ADD so v2 has one code.
    a1 = _add_agg_code(conn, segment_id=seg, version=1, code="A")
    v2, code_A = _review_add_and_finalize(
        conn, agg_code_id=a1, code_text="A", parent_version=1
    )

    # Now stage a second ADD against v2 without finalizing.
    a2 = _add_agg_code(conn, segment_id=seg, version=v2, code="B")
    code_B = _do_review_decision(
        source_agg_code_id=a2,
        parent_version=v2,
        decision=DECISION_ADD,
        new_code_text="B",
    )

    parent = db.get_codebook_with_codes_and_research_context(v2)
    live = live_codes_for_batch(parent)
    live_ids = {c.code_id for c in live}
    assert live_ids == {code_A, code_B}


# ---------------------------------------------------------------------------
# Per-code quote isolation
# ---------------------------------------------------------------------------


def test_quotes_are_scoped_per_code(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    a1 = _add_agg_code(
        conn, segment_id=seg, version=1, code="A", quote_texts=["qA"]
    )
    v2, code_A = _review_add_and_finalize(
        conn, agg_code_id=a1, code_text="A", parent_version=1
    )

    a2 = _add_agg_code(
        conn, segment_id=seg, version=v2, code="B", quote_texts=["qB"]
    )
    v3, code_B = _review_add_and_finalize(
        conn, agg_code_id=a2, code_text="B", parent_version=v2
    )

    entries = {e.code: e for e in _get_codebook_codes(v3)}
    assert [q["text"] for q in entries["A"].quotes] == ["qA"]
    assert [q["text"] for q in entries["B"].quotes] == ["qB"]
    a_qids = {q["quote_id"] for q in entries["A"].quotes}
    b_qids = {q["quote_id"] for q in entries["B"].quotes}
    assert a_qids.isdisjoint(b_qids)


# ---------------------------------------------------------------------------
# Aggregator-side
# ---------------------------------------------------------------------------


def test_load_aggregated_code_quotes_returns_aggregator_quotes(
    tmp_path: Path,
) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)
    agg = _add_agg_code(
        conn,
        segment_id=seg,
        version=1,
        code="agg",
        quote_texts=["one", "two", "three"],
    )

    quotes = db.aggregation.load_aggregated_code_quotes(agg)
    assert [q["text"] for q in quotes] == ["one", "two", "three"]
    qids = [q["quote_id"] for q in quotes]
    assert qids == sorted(qids)


def test_aggregator_emits_multiple_codes_with_distinct_quotes(
    tmp_path: Path,
) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    seg_obj = _segment_obj(seg)
    qa1 = db.add_quote(seg_obj, "a1")
    qa2 = db.add_quote(seg_obj, "a2")
    qb1 = db.add_quote(seg_obj, "b1")
    cb_version = db.latest_codebook().version
    alpha = Code(
        segment_id=seg,
        coder_id=SYSTEM_AGGREGATOR_ID,
        codebook_used_id=cb_version,
        code="alpha",
    )
    alpha.supporting_quotes = [qa1, qa2]
    beta = Code(
        segment_id=seg,
        coder_id=SYSTEM_AGGREGATOR_ID,
        codebook_used_id=cb_version,
        code="beta",
    )
    beta.supporting_quotes = [qb1]
    new_codes = save_aggregator_codes([alpha, beta])
    new_ids = [c.code_id for c in new_codes]

    quotes_alpha = db.aggregation.load_aggregated_code_quotes(new_ids[0])
    quotes_beta = db.aggregation.load_aggregated_code_quotes(new_ids[1])
    assert [q["text"] for q in quotes_alpha] == ["a1", "a2"]
    assert [q["text"] for q in quotes_beta] == ["b1"]


# ---------------------------------------------------------------------------
# Aggregator code that gets no review is still pending
# ---------------------------------------------------------------------------


def test_aggregator_code_without_review_is_pending(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)
    agg = _add_agg_code(conn, segment_id=seg, version=1, code="meh")

    nxt = next_aggregated_code_to_review()
    assert nxt is not None and nxt.code_id == agg
    assert _get_codebook_codes(1) == []


# ---------------------------------------------------------------------------
# Version lineage
# ---------------------------------------------------------------------------


def test_codebook_versions_chain_records_parent(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    a1 = _add_agg_code(conn, segment_id=seg, version=1, code="A")
    v2, _ = _review_add_and_finalize(
        conn, agg_code_id=a1, code_text="A", parent_version=1
    )
    a2 = _add_agg_code(conn, segment_id=seg, version=v2, code="B")
    v3, _ = _review_add_and_finalize(
        conn, agg_code_id=a2, code_text="B", parent_version=v2
    )

    versions = db.list_codebooks()
    by_version = {cv.version: cv for cv in versions}
    assert by_version[1].parent_version is None
    assert by_version[v2].parent_version == 1
    assert by_version[v3].parent_version == v2
