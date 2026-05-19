"""Tests for thematic_analysis_inc Step 4 (reviewer worker) — refactored schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from thematic_analysis.agents.reviewer import ReviewDecision, ReviewResult
from thematic_analysis.codebook import Quote

from thematic_analysis_inc import cli, workers
from thematic_analysis_inc import db as store


def _get_codebook_codes(version: int):
    """Adapter for the legacy CodebookEntry-style accessor used by tests."""
    import json
    snap = json.loads(store.codebook_to_json_for_version(version))
    class _E:
        def __init__(self, d): self.code = d["code"]
    return [_E(c) for c in snap.get("codes", [])]


def _seed_document(conn):
    return store.add_document("doc.md")


def _add_segments(conn, doc, n: int) -> list[int]:
    if isinstance(doc, int):
        from thematic_analysis_inc.db.connection import session
        from thematic_analysis_inc.db.models import Document
        with session() as s:
            doc = s.get(Document, doc)
            s.expunge(doc)
    rows = [(f"text {i}", 0, 0, i) for i in range(n)]
    segs = store.enqueue_segments(doc, rows)
    return [s.segment_id for s in segs]


@dataclass
class _StubAssignment:
    segment_id: str
    segment_text: str
    codes: list[str] = field(default_factory=list)
    rationales: list[str] = field(default_factory=list)
    is_new_code: list[bool] = field(default_factory=list)


class _StubCoder:
    def __init__(self, codebook, coder):
        pass

    def code_segment(self, segment_id, text):
        return _StubAssignment(
            segment_id=segment_id, segment_text=text,
            codes=["alpha", "beta"],
            rationales=["r1", "r2"],
            is_new_code=[True, True],
        )


class _StubAggregator:
    """Returns two retained codes: 'alpha' and 'beta'."""

    def __init__(self, codebook):
        pass

    def aggregate(self, assignments, apply_negotiation=True):
        from thematic_analysis.agents.aggregator import (
            AggregationResult,
            MergedCode,
        )

        seg_id = assignments[0].segment_id
        seg_text = assignments[0].segment_text
        return AggregationResult(
            merged_codes=[],
            retained_codes=[
                MergedCode(
                    code="alpha", original_codes=["alpha"],
                    quotes=[Quote(quote_id=seg_id, text=seg_text)],
                ),
                MergedCode(
                    code="beta", original_codes=["beta"],
                    quotes=[Quote(quote_id=seg_id, text=seg_text)],
                ),
            ],
        )


def _seed_ready_to_review(conn, n: int = 1) -> list[int]:
    store.add_coder("i")
    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, n)
    store.coding.enqueue_document(doc.document_id)
    for c in store.list_coders():
        while workers.code_one(
            conn, c.coder_id, use_mock_embeddings=True,
            agent_factory=lambda cb, x: _StubCoder(cb, x),
        ) is not None:
            pass
    while workers.aggregate_one(
        conn, use_mock_embeddings=True,
        agent_factory=lambda cb: _StubAggregator(cb),
    ) is not None:
        pass
    return sids


def _make_reviewer_factory(decision: ReviewDecision, target_code: str | None = None):
    class _StubReviewer:
        def __init__(self, codebook):
            self.codebook = codebook

        def review_code(self, code, quotes):
            return ReviewResult(
                code=code, decision=decision, target_code=target_code,
                rationale=f"stub-{decision.value}", quotes=quotes,
            )

        def apply_review(self, result):
            quotes = result.quotes or []
            if result.decision == ReviewDecision.ADD_NEW:
                self.codebook.add_code(result.code, quotes)
            elif result.decision == ReviewDecision.MERGE and result.target_code:
                for i, entry in enumerate(self.codebook.entries):
                    if entry.code == result.target_code:
                        self.codebook.add_quotes_to_code(i, quotes)
                        break
            elif result.decision == ReviewDecision.UPDATE and result.target_code:
                for i, entry in enumerate(self.codebook.entries):
                    if entry.code == result.target_code:
                        self.codebook.update_code(i, result.code)
                        self.codebook.add_quotes_to_code(i, quotes)
                        break

    return lambda cb: _StubReviewer(cb)


# ---------------------------------------------------------------------------
# review_one
# ---------------------------------------------------------------------------


def test_review_one_add_new_creates_version(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)
    v_before = store.latest_codebook().version

    res = workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.ADD_NEW),
    )
    assert res is not None and res["ok"] is True
    assert res["decision"] == "add_new"
    assert res["new_version"] is not None
    assert res["new_version"] > v_before

    cv = store.latest_codebook()
    assert cv.version == res["new_version"]
    codes_in_new = {
        e.code for e in _get_codebook_codes(cv.version)
    }
    assert res["code"] in codes_in_new

    # A reviewer code row should exist for this decision.
    row = conn.execute(
        "SELECT decision FROM codes_derived WHERE source_code_id = ? "
        "AND derivation_type = 'R'",
        (res["aggregated_code_id"],),
    ).fetchone()
    assert row["decision"] == "A"


def test_review_one_skip_leaves_no_trace(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)
    v_before = store.latest_codebook().version

    res = workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.SKIP),
    )
    assert res is not None and res["ok"] is True
    assert res["decision"] == "skip"
    assert res["new_version"] is None

    # Codebook version unchanged.
    assert store.latest_codebook().version == v_before

    # No codes_derived row for this aggregator code.
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM codes_derived WHERE source_code_id = ?",
        (res["aggregated_code_id"],),
    ).fetchone()["n"]
    assert n == 0


def test_review_one_merge_keeps_target(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    # Seed: ADD an 'existing-code' through a review so it's in the codebook.
    _seed_ready_to_review(conn, n=1)
    # First review (alpha) goes through as ADD with name 'existing-code'
    workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.ADD_NEW),
    )

    # The second aggregator code (beta) we MERGE into 'alpha'.
    res = workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(
            ReviewDecision.MERGE, target_code="alpha"
        ),
    )
    assert res is not None and res["ok"]
    assert res["decision"] == "merge"
    cv = store.latest_codebook()
    codes_in_new = {
        e.code for e in _get_codebook_codes(cv.version)
    }
    assert "alpha" in codes_in_new


def test_review_one_update_replaces_target(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)
    # First ADD 'alpha' to codebook.
    workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.ADD_NEW),
    )
    # Now UPDATE alpha → with the new code text from the reviewer (which is
    # the source aggregator code text, i.e. 'beta').
    res = workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(
            ReviewDecision.UPDATE, target_code="alpha"
        ),
    )
    assert res is not None and res["ok"]
    cv = store.latest_codebook()
    codes_in_new = {
        e.code for e in _get_codebook_codes(cv.version)
    }
    # 'alpha' should be gone, 'beta' (the reviewer's new code text) present.
    assert "alpha" not in codes_in_new
    assert "beta" in codes_in_new


def test_review_one_segment_done_after_all_reviewed_non_skip(
    tmp_path: Path,
) -> None:
    """ADD-only walkthrough: when every aggregator code has an outgoing
    review edge, the segment is `done`."""
    conn = store.init_db(tmp_path / "x.sqlite")
    sids = _seed_ready_to_review(conn, n=1)
    factory = _make_reviewer_factory(ReviewDecision.ADD_NEW)

    workers.review_one(conn, use_mock_embeddings=True, agent_factory=factory)
    s1 = store.status.derive_segment_status(sids[0])
    assert s1 == "reviewing"
    workers.review_one(conn, use_mock_embeddings=True, agent_factory=factory)
    s2 = store.status.derive_segment_status(sids[0])
    assert s2 == "done"


def test_review_one_skip_leaves_segment_reviewing(tmp_path: Path) -> None:
    """SKIP doesn't write a `codes_derived` edge, so the segment stays
    in `reviewing` until a non-SKIP decision settles it. This is the
    design intent — SKIPs aren't recorded."""
    conn = store.init_db(tmp_path / "x.sqlite")
    sids = _seed_ready_to_review(conn, n=1)
    factory = _make_reviewer_factory(ReviewDecision.SKIP)

    workers.review_one(conn, use_mock_embeddings=True, agent_factory=factory)
    workers.review_one(conn, use_mock_embeddings=True, agent_factory=factory)
    assert store.status.derive_segment_status(sids[0]) == "reviewing"


def test_review_one_returns_none_when_idle(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    assert workers.review_one(conn, use_mock_embeddings=True) is None


def test_drain_review_processes_all_codes(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=2)  # 2 segments × 2 codes = 4 codes

    counters = workers.drain_review(
        conn, use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.ADD_NEW),
    )
    assert counters == {"done": 4, "failed": 0}

    n_rev_codes = conn.execute(
        "SELECT COUNT(*) AS n FROM code WHERE coder_id = -1"
    ).fetchone()["n"]
    assert n_rev_codes == 4
