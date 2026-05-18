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
from thematic_analysis_inc.db.aggregation import (
    AggregatorMergeInput,
    record_aggregation_result,
)
from thematic_analysis_inc.db.review import (
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_UPDATE,
    next_aggregated_code_to_review,
    record_review,
    resolve_target_code_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init(tmp_path: Path):
    """Open a fresh DB and return the connection."""
    conn = db.init_db(tmp_path / "x.sqlite")
    return conn


def _seed_segment(conn, content: str = "seg content") -> int:
    doc_id = db.add_document(conn, "doc.md", b"x")
    db.enqueue_segments(conn, [(doc_id, content, 1, 5, 0)])
    row = conn.execute(
        "SELECT segment_id FROM segments ORDER BY segment_id DESC LIMIT 1"
    ).fetchone()
    return int(row["segment_id"])


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
    """Insert one aggregator code (coder_id=0) via the public helper.

    Returns the new aggregator code_id.
    """
    new_ids = record_aggregation_result(
        conn,
        segment_id=segment_id,
        version=version,
        inputs=[
            AggregatorMergeInput(
                code=code,
                description=description,
                rationale=rationale,
                quote_texts=list(quote_texts or []),
                source_code_ids=[],
            )
        ],
    )
    assert len(new_ids) == 1
    return new_ids[0]


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
    """Run a review-ADD and link any quote_texts onto the new reviewer code.

    Returns (new_version, new_reviewer_code_id).
    """
    new_version = record_review(
        conn,
        source_agg_code_id=agg_code_id,
        decision=DECISION_ADD,
        new_code_text=code_text,
        new_description=description,
        rationale=rationale,
        parent_version=parent_version,
    )
    new_code_id = _latest_reviewer_code_id(conn, code_text)
    for t in quote_texts or []:
        # quote rows are segment-scoped — use the aggregator code's segment
        seg_id = conn.execute(
            "SELECT segment_id FROM codes WHERE code_id = ?", (agg_code_id,)
        ).fetchone()["segment_id"]
        qid = db.add_quote(conn, seg_id, t)
        db.link_code_quote(conn, new_code_id, qid)
    return new_version, new_code_id


def _latest_reviewer_code_id(conn, code_text: str) -> int:
    row = conn.execute(
        "SELECT code_id FROM codes WHERE coder_id = -1 AND code = ? "
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
    assert db.get_codebook_codes(conn, 1) == []
    snap = json.loads(db.codebook_to_json_for_version(conn, 1))
    assert snap == {"codes": []}


def test_unknown_version_returns_empty(tmp_path: Path) -> None:
    conn = _init(tmp_path)
    # v42 doesn't exist; query is well-defined and returns no rows
    assert db.get_codebook_codes(conn, 42) == []
    assert json.loads(db.codebook_to_json_for_version(conn, 42)) == {
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
    assert db.get_codebook_codes(conn, 1) == []

    # v2 has exactly the new reviewer code with its quotes
    entries = db.get_codebook_codes(conn, 2)
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
    snap = json.loads(db.codebook_to_json_for_version(conn, 2))
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

    assert [e.code for e in db.get_codebook_codes(conn, 1)] == []
    assert [e.code for e in db.get_codebook_codes(conn, v2)] == ["A"]
    assert [e.code for e in db.get_codebook_codes(conn, v3)] == ["A", "B"]

    # Older version still snapshots exactly what it had at the time
    snap_v2 = json.loads(db.codebook_to_json_for_version(conn, v2))
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
    target_id = resolve_target_code_id(
        conn, version=v2, code_text="raw"
    )
    assert target_id is not None

    v3 = record_review(
        conn,
        source_agg_code_id=a2,
        decision=DECISION_UPDATE,
        new_code_text="refined",
        new_description="refined description",
        rationale="merged better wording",
        parent_version=v2,
        target_code_id=target_id,
    )

    v2_codes = [e.code for e in db.get_codebook_codes(conn, v2)]
    v3_codes = [e.code for e in db.get_codebook_codes(conn, v3)]
    assert v2_codes == ["raw"]      # untouched
    assert v3_codes == ["refined"]  # replaced

    # v3 entry has the new description; v2 entry still has the old
    v2_entry = db.get_codebook_codes(conn, v2)[0]
    v3_entry = db.get_codebook_codes(conn, v3)[0]
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
    target_id = resolve_target_code_id(conn, version=v2, code_text="orig")
    assert target_id == orig_id

    v3 = record_review(
        conn,
        source_agg_code_id=a2,
        decision=DECISION_MERGE,
        new_code_text="orig",
        new_description="",
        rationale="duplicate",
        parent_version=v2,
        target_code_id=target_id,
    )

    # Codebook membership unchanged across MERGE
    assert [e.code_id for e in db.get_codebook_codes(conn, v3)] == [orig_id]

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
    next_target = next_aggregated_code_to_review(conn)
    assert next_target is not None
    assert next_target.code_id == agg

    # codebook unchanged
    assert db.get_codebook_codes(conn, 1) == []


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

    entries = {e.code: e for e in db.get_codebook_codes(conn, v3)}
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
    doc_id = conn.execute(
        "SELECT document_id FROM segments WHERE segment_id = ?", (s1,)
    ).fetchone()["document_id"]
    db.enqueue_segments(conn, [(doc_id, "seg two content", 6, 10, 1)])
    s2 = int(
        conn.execute(
            "SELECT segment_id FROM segments WHERE document_id = ? "
            "ORDER BY segment_id DESC LIMIT 1",
            (doc_id,),
        ).fetchone()["segment_id"]
    )

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

    entries = {e.code: e for e in db.get_codebook_codes(conn, v3)}

    # Each quote belongs to the right segment
    x_qid = int(entries["X"].quotes[0]["quote_id"])
    y_qid = int(entries["Y"].quotes[0]["quote_id"])
    seg_for = {
        int(r["quote_id"]): int(r["segment_id"])
        for r in conn.execute(
            "SELECT quote_id, segment_id FROM quotes WHERE quote_id IN (?, ?)",
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

    quotes = db.aggregation.load_aggregated_code_quotes(conn, agg)
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

    new_ids = record_aggregation_result(
        conn,
        segment_id=seg,
        version=1,
        inputs=[
            AggregatorMergeInput(
                code="alpha",
                description="first",
                rationale="r1",
                quote_texts=["a1", "a2"],
                source_code_ids=[],
            ),
            AggregatorMergeInput(
                code="beta",
                description="second",
                rationale="r2",
                quote_texts=["b1"],
                source_code_ids=[],
            ),
        ],
    )
    assert len(new_ids) == 2

    quotes_alpha = db.aggregation.load_aggregated_code_quotes(
        conn, new_ids[0]
    )
    quotes_beta = db.aggregation.load_aggregated_code_quotes(
        conn, new_ids[1]
    )
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

    versions = db.list_codebook_versions(conn)
    by_version = {cv.version: cv for cv in versions}
    assert by_version[1].parent_version is None
    assert by_version[v2].parent_version == 1
    assert by_version[v3].parent_version == v2
    # `created_by` distinguishes init vs reviewer rows
    assert by_version[1].created_by == "init"
    assert by_version[v2].created_by == "reviewer"
    assert by_version[v3].created_by == "reviewer"
