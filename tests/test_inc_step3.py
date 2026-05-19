"""Tests for thematic_analysis_inc Step 3 (aggregator worker) — refactored schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from thematic_analysis.agents.aggregator import AggregationResult, MergedCode
from thematic_analysis.codebook import Quote

from thematic_analysis_inc import cli, workers
from thematic_analysis_inc import db as store


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
        self.coder = coder

    def code_segment(self, segment_id, text):
        return _StubAssignment(
            segment_id=segment_id,
            segment_text=text,
            codes=[f"c{self.coder.coder_id}-only", "shared"],
            rationales=["rA", "rB"],
            is_new_code=[True, False],
        )


def _coder_factory():
    def f(cb, coder):
        return _StubCoder(cb, coder)
    return f


class _StubAggregator:
    """Merges all `*-only` codes into 'unique' and retains 'shared'."""

    def __init__(self, codebook):
        self.codebook = codebook
        self.last_assignments = None

    def aggregate(self, assignments, apply_negotiation=True):
        self.last_assignments = assignments
        all_unique = sorted(
            {c for a in assignments for c in a.codes if c.endswith("-only")}
        )
        seg_id = assignments[0].segment_id
        seg_text = assignments[0].segment_text
        merged = MergedCode(
            code="unique",
            original_codes=all_unique,
            quotes=[Quote(quote_id=seg_id, text=seg_text)],
            merge_rationale="combined uniques",
        )
        retained = MergedCode(
            code="shared",
            original_codes=["shared"],
            quotes=[Quote(quote_id=seg_id, text=seg_text)],
        )
        return AggregationResult(merged_codes=[merged], retained_codes=[retained])


def _agg_factory():
    def f(cb):
        return _StubAggregator(cb)
    return f


def _seed_two_coders_done(conn, n: int = 1) -> list[int]:
    store.add_coder("id-a")
    store.add_coder("id-b")
    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, n)
    for c in store.list_coders():
        while workers.code_one(conn, c.coder_id, agent_factory=_coder_factory()) is not None:
            pass
    return sids


# ---------------------------------------------------------------------------
# next_segment_to_aggregate
# ---------------------------------------------------------------------------


def test_next_segment_requires_all_coders_done(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    a = store.add_coder("i")
    store.add_coder("i")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 1)
    workers.code_one(conn, a.coder_id, agent_factory=_coder_factory())
    assert store.aggregation.next_segment_to_aggregate() is None
    # finish bob too
    for c in store.list_coders():
        while workers.code_one(conn, c.coder_id, agent_factory=_coder_factory()) is not None:
            pass
    row = store.aggregation.next_segment_to_aggregate()
    assert row is not None


def test_next_segment_none_with_no_coders(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 1)
    assert store.aggregation.next_segment_to_aggregate() is None


def test_next_segment_excludes_already_aggregated(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    sids = _seed_two_coders_done(conn, n=1)
    workers.aggregate_one(conn, use_mock_embeddings=True, agent_factory=_agg_factory())
    assert store.aggregation.next_segment_to_aggregate() is None
    # Aggregator codes exist for this segment.
    assert store.aggregation.segment_has_aggregator_code(sids[0])


# ---------------------------------------------------------------------------
# aggregate_one
# ---------------------------------------------------------------------------


def test_aggregate_one_persists_codes_and_provenance(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    sids = _seed_two_coders_done(conn, n=1)

    res = workers.aggregate_one(
        conn, use_mock_embeddings=True, agent_factory=_agg_factory()
    )
    assert res is not None and res["ok"] is True
    assert res["n_merged"] == 1
    assert res["n_retained"] == 1
    assert res["n_in"] == 4  # 2 coders × 2 codes

    rows = conn.execute(
        "SELECT code FROM code WHERE coder_id = 0 ORDER BY code"
    ).fetchall()
    assert {r["code"] for r in rows} == {"shared", "unique"}

    # Provenance: for the 'unique' aggregator code, source_code_ids should
    # span both coders' '-only' codes.
    rows = conn.execute(
        "SELECT c.code, COUNT(d.source_code_id) AS n_src "
        "FROM code c LEFT JOIN codes_derived d "
        "  ON d.new_code_id = c.code_id AND d.derivation_type = 'A' "
        "WHERE c.coder_id = 0 GROUP BY c.code"
    ).fetchall()
    by_code = {r["code"]: r["n_src"] for r in rows}
    assert by_code["unique"] == 2
    assert by_code["shared"] == 2

    # Quotes attached via codes_supporting_quotes.
    n_links = conn.execute(
        "SELECT COUNT(*) AS n FROM codes_supporting_quotes csq "
        "JOIN code c ON c.code_id = csq.code_id WHERE c.coder_id = 0"
    ).fetchone()["n"]
    assert n_links >= 2

    # Derived segment status now reads as 'reviewing'.
    s = store.status.derive_segment_status(sids[0])
    assert s == "reviewing"


def test_aggregate_one_returns_none_when_idle(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    assert workers.aggregate_one(conn, agent_factory=_agg_factory()) is None


def test_aggregate_one_empty_result_marks_segment_done(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    sids = _seed_two_coders_done(conn, n=1)

    class _Empty:
        def __init__(self, cb): pass
        def aggregate(self, assignments, apply_negotiation=True):
            return AggregationResult(merged_codes=[], retained_codes=[])

    res = workers.aggregate_one(conn, agent_factory=lambda cb: _Empty(cb))
    assert res is not None and res["ok"]
    # With no aggregator codes the segment can't progress to reviewing — it
    # stays at "aggregating" in the derived view (no agg codes recorded).
    s = store.status.derive_segment_status(sids[0])
    assert s == "aggregating"


def test_drain_aggregate_processes_all_segments(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_two_coders_done(conn, n=3)

    counters = workers.drain_aggregate(
        conn, agent_factory=_agg_factory()
    )
    assert counters == {"done": 3, "failed": 0}
    n_agg_segments = conn.execute(
        "SELECT COUNT(DISTINCT segment_id) AS n FROM code WHERE coder_id = 0"
    ).fetchone()["n"]
    assert n_agg_segments == 3


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_aggregate_runs_against_stub(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "alice-identity"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "bob-identity"]) == 0

    conn = store.connect(db)
    doc = _seed_document(conn)
    _add_segments(conn, doc, 2)
    conn.close()

    monkeypatch.setattr(workers, "default_coder_factory", _coder_factory())
    monkeypatch.setattr(
        workers, "default_aggregator_factory", lambda cb: _StubAggregator(cb)
    )

    # Reviewer is the same stub used in step4; here we just need a no-op
    # reviewer to avoid network. Use ADD_NEW so the codebook grows.
    from thematic_analysis.agents.reviewer import ReviewDecision, ReviewResult

    class _Reviewer:
        def __init__(self, cb): self.codebook = cb
        def review_code(self, code, quotes):
            return ReviewResult(
                code=code, decision=ReviewDecision.ADD_NEW,
                rationale="ok", quotes=quotes,
            )
        def apply_review(self, result):
            self.codebook.add_code(result.code, result.quotes or [])

    monkeypatch.setattr(workers, "default_reviewer_factory", lambda cb: _Reviewer(cb))

    assert cli.main(
        ["--db", str(db), "code", "1", "--mock-embeddings"]
    ) == 0
    assert cli.main(
        ["--db", str(db), "code", "2", "--mock-embeddings"]
    ) == 0
    capsys.readouterr()

    rc = cli.main(["--db", str(db), "update-codebook", "--mock-embeddings"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "merged=" in out and "retained=" in out
