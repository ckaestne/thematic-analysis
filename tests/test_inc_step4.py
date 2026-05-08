"""Tests for thematic_analysis_inc Step 4 (reviewer worker)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from thematic_analysis.agents.reviewer import ReviewDecision, ReviewResult
from thematic_analysis.codebook import Codebook, Quote

from thematic_analysis_inc import cli, store, workers


# ---------------------------------------------------------------------------
# Helpers to seed the DB through coding + aggregation
# ---------------------------------------------------------------------------


def _segments(n: int) -> list[tuple[str, str]]:
    return [(f"seg_{i:04d}", f"text {i}") for i in range(n)]


@dataclass
class _StubAssignment:
    segment_id: str
    segment_text: str
    codes: list[str] = field(default_factory=list)
    rationales: list[str] = field(default_factory=list)
    is_new_code: list[bool] = field(default_factory=list)


class _StubCoder:
    def __init__(self, codebook, coder):
        self.coder = coder

    def code_segment(self, segment_id, text):
        return _StubAssignment(
            segment_id=segment_id,
            segment_text=text,
            codes=["alpha", "beta"],
            rationales=["r1", "r2"],
            is_new_code=[True, True],
        )


class _StubAggregator:
    """Returns two retained codes: 'alpha' and 'beta'."""

    def __init__(self, codebook):
        pass

    def aggregate(self, assignments, apply_negotiation=True):
        from thematic_analysis.agents.aggregator import AggregationResult, MergedCode

        seg_id = assignments[0].segment_id
        seg_text = assignments[0].segment_text
        return AggregationResult(
            merged_codes=[],
            retained_codes=[
                MergedCode(
                    code="alpha",
                    original_codes=["alpha"],
                    quotes=[Quote(quote_id=seg_id, text=seg_text)],
                ),
                MergedCode(
                    code="beta",
                    original_codes=["beta"],
                    quotes=[Quote(quote_id=seg_id, text=seg_text)],
                ),
            ],
        )


def _seed_ready_to_review(conn, n: int = 1) -> None:
    """Seed the DB so n segments have aggregated_codes ready for review."""
    store.add_coder(conn, "alice", "identity-a")
    store.enqueue_segments(conn, _segments(n))
    while workers.code_one(
        conn, "alice",
        use_mock_embeddings=True,
        agent_factory=lambda cb, c: _StubCoder(cb, c),
    ) is not None:
        pass
    while workers.aggregate_one(
        conn,
        use_mock_embeddings=True,
        agent_factory=lambda cb: _StubAggregator(cb),
    ) is not None:
        pass


# ---------------------------------------------------------------------------
# Stub ReviewerAgent factories — one per decision type
# ---------------------------------------------------------------------------


def _make_reviewer_factory(decision: ReviewDecision, target_code: str | None = None):
    """Return an agent_factory that always returns the given decision."""

    class _StubReviewer:
        def __init__(self, codebook):
            self.codebook = codebook

        def review_code(self, code, quotes):
            return ReviewResult(
                code=code,
                decision=decision,
                target_code=target_code,
                rationale=f"stub-{decision.value}",
                quotes=quotes,
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
            # SKIP: do nothing

    return lambda cb: _StubReviewer(cb)


# ---------------------------------------------------------------------------
# review_one — per-decision-type tests
# ---------------------------------------------------------------------------


def test_review_one_add_new_creates_version(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)

    v_before = store.latest_codebook_version(conn).version

    res = workers.review_one(
        conn,
        use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.ADD_NEW),
    )

    assert res is not None and res["ok"] is True
    assert res["decision"] == "add_new"
    assert res["new_version"] is not None
    assert res["new_version"] > v_before

    # New codebook version should contain the reviewed code
    cv = store.latest_codebook_version(conn)
    assert cv.version == res["new_version"]
    codes_in_new = [c["code"] for c in json.loads(cv.snapshot_json)["codes"]]
    assert res["code"] in codes_in_new

    # review_decisions row inserted, applied=1
    rd = conn.execute(
        "SELECT decision, applied, resulting_version FROM review_decisions "
        "WHERE aggregated_code_id = ?",
        (res["aggregated_code_id"],),
    ).fetchone()
    assert rd["decision"] == "add_new"
    assert rd["applied"] == 1
    assert rd["resulting_version"] == res["new_version"]


def test_review_one_skip_creates_no_version(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)

    v_before = store.latest_codebook_version(conn).version

    res = workers.review_one(
        conn,
        use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.SKIP),
    )

    assert res is not None and res["ok"] is True
    assert res["decision"] == "skip"
    assert res["new_version"] is None

    # Codebook version must NOT have changed
    assert store.latest_codebook_version(conn).version == v_before

    rd = conn.execute(
        "SELECT decision, applied, resulting_version FROM review_decisions "
        "WHERE aggregated_code_id = ?",
        (res["aggregated_code_id"],),
    ).fetchone()
    assert rd["decision"] == "skip"
    assert rd["resulting_version"] is None


def test_review_one_merge_updates_codebook(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    # Seed a code into the codebook so we have something to merge into.
    initial_cb = Codebook(use_mock_embeddings=True)
    initial_cb.add_code("existing-code", [])
    v1 = store.insert_codebook_version(
        conn, initial_cb.to_json(), parent=1, created_by="test"
    )
    workers.clear_codebook_cache()

    _seed_ready_to_review(conn, n=1)

    res = workers.review_one(
        conn,
        use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(
            ReviewDecision.MERGE, target_code="existing-code"
        ),
    )

    assert res is not None and res["ok"]
    assert res["decision"] == "merge"
    assert res["new_version"] is not None

    cv = store.latest_codebook_version(conn)
    codes_in_new = {c["code"] for c in json.loads(cv.snapshot_json)["codes"]}
    assert "existing-code" in codes_in_new


def test_review_one_update_updates_codebook(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    initial_cb = Codebook(use_mock_embeddings=True)
    initial_cb.add_code("old-name", [])
    store.insert_codebook_version(
        conn, initial_cb.to_json(), parent=1, created_by="test"
    )
    workers.clear_codebook_cache()

    _seed_ready_to_review(conn, n=1)

    # Review 'alpha' with UPDATE targeting 'old-name' -> renames to 'alpha'
    res = workers.review_one(
        conn,
        use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(
            ReviewDecision.UPDATE, target_code="old-name"
        ),
    )

    assert res is not None and res["ok"]
    assert res["decision"] == "update"
    assert res["new_version"] is not None

    cv = store.latest_codebook_version(conn)
    codes_in_new = {c["code"] for c in json.loads(cv.snapshot_json)["codes"]}
    # old-name renamed to 'alpha'
    assert "alpha" in codes_in_new
    assert "old-name" not in codes_in_new


def test_review_one_marks_segment_done_after_all_codes_reviewed(
    tmp_path: Path,
) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)

    # Two aggregated_codes (alpha, beta). Both must be reviewed before 'done'.
    factory = _make_reviewer_factory(ReviewDecision.SKIP)

    res1 = workers.review_one(conn, use_mock_embeddings=True, agent_factory=factory)
    assert res1 is not None and res1["ok"]
    seg_status = conn.execute(
        "SELECT status FROM segments WHERE segment_id = 'seg_0000'"
    ).fetchone()["status"]
    assert seg_status == "reviewing"  # still one code left

    res2 = workers.review_one(conn, use_mock_embeddings=True, agent_factory=factory)
    assert res2 is not None and res2["ok"]
    seg_status = conn.execute(
        "SELECT status FROM segments WHERE segment_id = 'seg_0000'"
    ).fetchone()["status"]
    assert seg_status == "done"


def test_review_one_returns_none_when_idle(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    assert workers.review_one(conn, use_mock_embeddings=True) is None


def test_drain_review_processes_all_codes(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=2)  # 2 segments × 2 codes = 4 codes

    counters = workers.drain_review(
        conn,
        use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.ADD_NEW),
    )
    assert counters == {"done": 4, "failed": 0}

    n_rd = conn.execute(
        "SELECT COUNT(*) AS n FROM review_decisions"
    ).fetchone()["n"]
    assert n_rd == 4

    # Both segments must be done
    n_done = conn.execute(
        "SELECT COUNT(*) AS n FROM segments WHERE status='done'"
    ).fetchone()["n"]
    assert n_done == 2


def test_drain_review_codebook_grows_incrementally(tmp_path: Path) -> None:
    """Each ADD_NEW decision should bump the version and grow the codebook."""
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)  # 2 codes: alpha, beta

    workers.drain_review(
        conn,
        use_mock_embeddings=True,
        agent_factory=_make_reviewer_factory(ReviewDecision.ADD_NEW),
    )

    cv = store.latest_codebook_version(conn)
    codes = [c["code"] for c in json.loads(cv.snapshot_json)["codes"]]
    assert "alpha" in codes
    assert "beta" in codes
    # Version bumped twice (once per ADD_NEW decision)
    assert cv.version >= 3  # v1 (init) + v2 (alpha) + v3 (beta)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_review_runs_against_stub(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "alice", "i"]) == 0

    conn = store.connect(db)
    store.enqueue_segments(conn, _segments(1))
    conn.close()

    monkeypatch.setattr(
        workers, "default_coder_factory", lambda cb, c: _StubCoder(cb, c)
    )
    monkeypatch.setattr(
        workers, "default_aggregator_factory", lambda cb: _StubAggregator(cb)
    )
    monkeypatch.setattr(
        workers,
        "default_reviewer_factory",
        _make_reviewer_factory(ReviewDecision.ADD_NEW),
    )

    assert cli.main(["--db", str(db), "code", "alice", "--mock-embeddings"]) == 0
    assert cli.main(["--db", str(db), "aggregate", "--mock-embeddings"]) == 0
    capsys.readouterr()

    rc = cli.main(["--db", str(db), "review", "--mock-embeddings"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "done: 2 ok" in out  # 2 codes (alpha + beta) reviewed
    assert "add_new" in out
