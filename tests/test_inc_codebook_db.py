"""Tests for the codebook-as-JSON view across versions.

These tests exercise the data-access layer in `thematic_analysis_inc.db`
end-to-end: they build codes / quotes / codebook versions through the
public API (`record_aggregation_result`, `record_review`, `add_quote`,
`link_code_quote`, …) and verify what `get_codebook_codes` and
`codebook_to_json_for_version` return for each version.

Focus: collecting all the data needed to print a codebook (codes,
descriptions, rationales, supporting quotes) and confirming that
older codebook versions stay stable when newer ones are added.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thematic_analysis_inc import db
from thematic_analysis_inc.db.aggregation import record_aggregation_result
from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID
from thematic_analysis_inc.db.models import Code, Quote
from thematic_analysis_inc.db.review import (
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_UPDATE,
    next_aggregated_code_to_review,
    record_review,
    resolve_target_code,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init(tmp_path: Path):
    """Open a fresh DB and return the connection."""
    conn = db.init_db(tmp_path / "x.sqlite")
    return conn


def _get_codebook_codes(version: int):
    """Return CodebookEntry-like objects with .code, .code_id, .description,
    .rationale, .quotes — mirroring the legacy ``db.get_codebook_codes``."""
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


def _code_obj(code_id: int):
    from thematic_analysis_inc.db.connection import session
    from thematic_analysis_inc.db.models import Code
    with session() as s:
        c = s.get(Code, code_id)
        if c is not None:
            s.expunge(c)
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
    """Insert one aggregator code (coder_id=0). Returns the new code_id."""
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
    new_codes = record_aggregation_result(
        seg, [new_code], codebook_version=version
    )
    assert len(new_codes) == 1
    return new_codes[0].code_id


def _review_add(
    conn,
    *,
    agg_code_id: int,
    code_text: str,
    description: str = "",
    rationale: str = "",
    parent_version: int,
    quote_texts: list[str] | None = None,
) -> tuple[int, int]:
    """Run a review-ADD; link quotes onto the new reviewer code.

    Returns (new_version, new_reviewer_code_id).
    """
    parent_cb = db.get_codebook(parent_version)
    agg_code = _code_obj(agg_code_id)
    new_cb = record_review(
        source_agg_code=agg_code,
        decision=DECISION_ADD,
        new_code_text=code_text,
        new_description=description,
        rationale=rationale,
        parent_codebook=parent_cb,
    )
    new_code_id = _latest_reviewer_code_id(conn, code_text)
    new_code = _code_obj(new_code_id)
    seg = _segment_obj(agg_code.segment_id)
    for t in quote_texts or []:
        q = db.add_quote(seg, t)
        db.link_code_quote(new_code, q)
    return new_cb.version, new_code_id


def _latest_reviewer_code_id(conn, code_text: str) -> int:
    row = conn.execute(
        "SELECT code_id FROM code WHERE coder_id = -1 AND code = ? "
        "ORDER BY code_id DESC LIMIT 1",
        (code_text,),
    ).fetchone()
    assert row is not None, f"reviewer code {code_text!r} not found"
    return int(row["code_id"])


# ---------------------------------------------------------------------------
# Empty codebook
# ---------------------------------------------------------------------------


def test_empty_codebook_v1_json_is_empty(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    assert _get_codebook_codes(1) == []
    snap = json.loads(db.codebook_to_json_for_version(1))
    assert snap == {"codes": []}


def test_unknown_version_returns_empty(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    # v42 doesn't exist; query is well-defined and returns no rows
    assert _get_codebook_codes(42) == []
    assert json.loads(db.codebook_to_json_for_version(42)) == {
        "codes": []
    }


# ---------------------------------------------------------------------------
# Single ADD: one code with quotes shows up in the new version's JSON
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

    new_version, new_code_id = _review_add(
        conn,
        agg_code_id=agg,
        code_text="resistance",
        description="opposing change",
        rationale="reviewer wants this in the codebook",
        parent_version=1,
        quote_texts=["I refuse to change", "no way"],
    )
    assert new_version == 2

    # v1 still empty
    assert _get_codebook_codes(1) == []

    # v2 has exactly the new reviewer code with its quotes
    entries = _get_codebook_codes(2)
    assert len(entries) == 1
    e = entries[0]
    assert e.code_id == new_code_id
    assert e.code == "resistance"
    assert e.description == "opposing change"
    assert e.rationale == "reviewer wants this in the codebook"
    assert [q["text"] for q in e.quotes] == ["I refuse to change", "no way"]
    # quote_ids are stringified ints, monotonic in insertion order
    assert all(int(q["quote_id"]) > 0 for q in e.quotes)


def test_codebook_json_shape_matches_legacy_contract(tmp_path: Path) -> None:
    """`codebook_to_json_for_version` must keep producing the same shape
    that `Codebook.from_json` consumes: `{"codes": [{"code", "quotes"}]}`."""
    conn = _init(tmp_path)
    seg = _seed_segment(conn)
    agg = _add_agg_code(
        conn, segment_id=seg, version=1, code="c1", quote_texts=["q1"]
    )
    _review_add(
        conn,
        agg_code_id=agg,
        code_text="c1",
        parent_version=1,
        quote_texts=["q1"],
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
    v2, _ = _review_add(
        conn, agg_code_id=a1, code_text="A",
        parent_version=1, quote_texts=["qa"],
    )
    a2 = _add_agg_code(
        conn, segment_id=seg, version=v2, code="B", quote_texts=["qb"]
    )
    v3, _ = _review_add(
        conn, agg_code_id=a2, code_text="B",
        parent_version=v2, quote_texts=["qb"],
    )

    assert [e.code for e in _get_codebook_codes(1)] == []
    assert [e.code for e in _get_codebook_codes(v2)] == ["A"]
    assert [e.code for e in _get_codebook_codes(v3)] == ["A", "B"]

    # Older version still snapshots exactly what it had at the time
    snap_v2 = json.loads(db.codebook_to_json_for_version(v2))
    assert [c["code"] for c in snap_v2["codes"]] == ["A"]


# ---------------------------------------------------------------------------
# UPDATE: replaces target code in the new version, older versions unchanged
# ---------------------------------------------------------------------------


def test_update_replaces_code_in_new_version_only(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    a1 = _add_agg_code(
        conn, segment_id=seg, version=1, code="raw", quote_texts=["q1"]
    )
    v2, _ = _review_add(
        conn, agg_code_id=a1, code_text="raw",
        parent_version=1, quote_texts=["q1"],
    )

    # Aggregator for a second agg code at v2 → reviewer UPDATEs the existing
    # "raw" entry to "refined".
    a2 = _add_agg_code(
        conn, segment_id=seg, version=v2, code="refined-source",
        quote_texts=["q2"],
    )
    target = resolve_target_code(db.get_codebook(v2), "raw")
    assert target is not None

    new_cb = record_review(
        source_agg_code=_code_obj(a2),
        decision=DECISION_UPDATE,
        new_code_text="refined",
        new_description="refined description",
        rationale="merged better wording",
        parent_codebook=db.get_codebook(v2),
        target_code=target,
    )
    v3 = new_cb.version

    v2_codes = [e.code for e in _get_codebook_codes(v2)]
    v3_codes = [e.code for e in _get_codebook_codes(v3)]
    assert v2_codes == ["raw"]      # untouched
    assert v3_codes == ["refined"]  # replaced

    # v3 entry has the new description; v2 entry still has the old
    v2_entry = _get_codebook_codes(v2)[0]
    v3_entry = _get_codebook_codes(v3)[0]
    assert v2_entry.description == ""
    assert v3_entry.description == "refined description"


# ---------------------------------------------------------------------------
# MERGE: target stays in codebook, derivation edge is recorded, new code is
# *not* in the codebook
# ---------------------------------------------------------------------------


def test_merge_keeps_target_and_records_derivation_edge(
    tmp_path: Path,
) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    a1 = _add_agg_code(
        conn, segment_id=seg, version=1, code="orig",
    )
    v2, orig_id = _review_add(
        conn, agg_code_id=a1, code_text="orig", parent_version=1
    )

    a2 = _add_agg_code(
        conn, segment_id=seg, version=v2, code="dup-source"
    )
    target = resolve_target_code(db.get_codebook(v2), "orig")
    assert target is not None and target.code_id == orig_id

    new_cb = record_review(
        source_agg_code=_code_obj(a2),
        decision=DECISION_MERGE,
        new_code_text="orig",
        new_description="",
        rationale="duplicate",
        parent_codebook=db.get_codebook(v2),
        target_code=target,
    )
    v3 = new_cb.version

    # Codebook membership unchanged across MERGE
    assert [e.code_id for e in _get_codebook_codes(v3)] == [orig_id]

    # ...but a derivation edge was recorded
    row = conn.execute(
        "SELECT decision, rationale FROM codes_derived "
        "WHERE source_code_id = ? AND derivation_type = 'R'",
        (a2,),
    ).fetchone()
    assert row is not None
    assert row["decision"] == DECISION_MERGE
    assert row["rationale"] == "duplicate"


# ---------------------------------------------------------------------------
# SKIP: by design leaves no DB trace; the aggregator code is still
# "available to review"
# ---------------------------------------------------------------------------


def test_skip_leaves_aggregator_code_available(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)
    agg = _add_agg_code(conn, segment_id=seg, version=1, code="meh")

    # No SKIP function exists in `db.review`; SKIP == no call. So the
    # aggregator code still has no outgoing 'R' edge.
    next_target = next_aggregated_code_to_review()
    assert next_target is not None
    assert next_target.code_id == agg

    # codebook unchanged
    assert _get_codebook_codes(1) == []


# ---------------------------------------------------------------------------
# Per-code quote isolation: quotes attached to one code don't leak to
# others, even on the same segment.
# ---------------------------------------------------------------------------


def test_quotes_are_scoped_per_code(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    a1 = _add_agg_code(
        conn, segment_id=seg, version=1, code="A", quote_texts=["qA"]
    )
    v2, code_A = _review_add(
        conn, agg_code_id=a1, code_text="A",
        parent_version=1, quote_texts=["qA"],
    )

    a2 = _add_agg_code(
        conn, segment_id=seg, version=v2, code="B", quote_texts=["qB"]
    )
    v3, code_B = _review_add(
        conn, agg_code_id=a2, code_text="B",
        parent_version=v2, quote_texts=["qB1", "qB2"],
    )

    entries = {e.code: e for e in _get_codebook_codes(v3)}
    assert [q["text"] for q in entries["A"].quotes] == ["qA"]
    assert [q["text"] for q in entries["B"].quotes] == ["qB1", "qB2"]
    # No quote_id appears under both codes
    a_qids = {q["quote_id"] for q in entries["A"].quotes}
    b_qids = {q["quote_id"] for q in entries["B"].quotes}
    assert a_qids.isdisjoint(b_qids)


# ---------------------------------------------------------------------------
# Quotes from multiple segments end up on different codes
# ---------------------------------------------------------------------------


def test_quotes_from_different_segments(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    s1 = _seed_segment(conn, "seg one content")
    # Add a second segment under the same document
    seg1 = db.get_segment(s1)
    from thematic_analysis_inc.db.connection import session
    from thematic_analysis_inc.db.models import Document
    with session() as ss:
        doc = ss.get(Document, seg1.document_id)
        ss.expunge(doc)
    new_segs = db.enqueue_segments(doc, [(None, "seg two content", 6, 10, 1)])
    s2 = new_segs[0].segment_id

    a1 = _add_agg_code(
        conn, segment_id=s1, version=1, code="X", quote_texts=["x-q"]
    )
    v2, _ = _review_add(
        conn, agg_code_id=a1, code_text="X",
        parent_version=1, quote_texts=["x-q"],
    )
    a2 = _add_agg_code(
        conn, segment_id=s2, version=v2, code="Y", quote_texts=["y-q"]
    )
    v3, _ = _review_add(
        conn, agg_code_id=a2, code_text="Y",
        parent_version=v2, quote_texts=["y-q"],
    )

    entries = {e.code: e for e in _get_codebook_codes(v3)}

    # Each quote belongs to the right segment
    x_qid = int(entries["X"].quotes[0]["quote_id"])
    y_qid = int(entries["Y"].quotes[0]["quote_id"])
    seg_for = {
        int(r["quote_id"]): int(r["segment_id"])
        for r in conn.execute(
            "SELECT quote_id, segment_id FROM quote WHERE quote_id IN (?, ?)",
            (x_qid, y_qid),
        ).fetchall()
    }
    assert seg_for[x_qid] == s1
    assert seg_for[y_qid] == s2


# ---------------------------------------------------------------------------
# Aggregator-side: quotes attached during aggregation are visible to the
# review step via `load_aggregated_code_quotes`
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
    # quote_ids are ascending
    qids = [q["quote_id"] for q in quotes]
    assert qids == sorted(qids)


# ---------------------------------------------------------------------------
# Multiple aggregator codes per segment: ordering in codebook follows
# code_id (insertion order); each carries only its own quotes
# ---------------------------------------------------------------------------


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
        description="first",
        rationale="r1",
    )
    alpha.supporting_quotes = [qa1, qa2]
    beta = Code(
        segment_id=seg,
        coder_id=SYSTEM_AGGREGATOR_ID,
        codebook_used_id=cb_version,
        code="beta",
        description="second",
        rationale="r2",
    )
    beta.supporting_quotes = [qb1]
    new_codes = record_aggregation_result(seg_obj, [alpha, beta])
    new_ids = [c.code_id for c in new_codes]
    assert len(new_ids) == 2

    quotes_alpha = db.aggregation.load_aggregated_code_quotes(new_ids[0])
    quotes_beta = db.aggregation.load_aggregated_code_quotes(new_ids[1])
    assert [q["text"] for q in quotes_alpha] == ["a1", "a2"]
    assert [q["text"] for q in quotes_beta] == ["b1"]


# ---------------------------------------------------------------------------
# Version lineage: parent_version chain is queryable
# ---------------------------------------------------------------------------


def test_codebook_versions_chain_records_parent(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    seg = _seed_segment(conn)

    a1 = _add_agg_code(conn, segment_id=seg, version=1, code="A")
    v2, _ = _review_add(
        conn, agg_code_id=a1, code_text="A", parent_version=1
    )
    a2 = _add_agg_code(conn, segment_id=seg, version=v2, code="B")
    v3, _ = _review_add(
        conn, agg_code_id=a2, code_text="B", parent_version=v2
    )

    versions = db.list_codebooks()
    by_version = {cv.version: cv for cv in versions}
    assert by_version[1].parent_version is None
    assert by_version[v2].parent_version == 1
    assert by_version[v3].parent_version == v2
    # `created_by` column was dropped in the SQLModel migration; the
    # parent_version chain is the canonical lineage instead.
