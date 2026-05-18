"""Tests for thematic_analysis_inc Step 3 (aggregator worker)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from thematic_analysis.agents.aggregator import AggregationResult, MergedCode
from thematic_analysis.codebook import Quote

from thematic_analysis_inc import cli, store, workers


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
        # Each coder produces a unique label plus a shared one.
        return _StubAssignment(
            segment_id=segment_id,
            segment_text=text,
            codes=[f"{self.coder.coder_id}-only", "shared"],
            rationales=["rA", "rB"],
            is_new_code=[True, False],
        )


def _coder_factory():
    def f(cb, coder):
        return _StubCoder(cb, coder)
    return f


class _StubAggregator:
    """Aggregator that merges all `*-only` labels into 'unique' and retains
    the 'shared' code."""

    def __init__(self, codebook):
        self.codebook = codebook
        self.last_assignments = None

    def aggregate(self, assignments, apply_negotiation=True):
        self.last_assignments = assignments
        # Collect unique codes per assignment to source them properly.
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


def _seed_two_coders_done(conn, n: int = 2) -> None:
    store.add_coder(conn, "alice", "id-a")
    store.add_coder(conn, "bob", "id-b")
    store.enqueue_segments(conn, _segments(n))
    for cid in ("alice", "bob"):
        while workers.code_one(conn, cid, agent_factory=_coder_factory()) is not None:
            pass


# ---------------------------------------------------------------------------
# next_segment_to_aggregate
# ---------------------------------------------------------------------------


def test_next_segment_requires_all_coders_done(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    store.add_coder(conn, "alice", "i")
    store.add_coder(conn, "bob", "i")
    store.enqueue_segments(conn, _segments(1))
    # Only alice has coded the segment.
    workers.code_one(conn, "alice", agent_factory=_coder_factory())
    assert store.next_segment_to_aggregate(conn) is None
    workers.code_one(conn, "bob", agent_factory=_coder_factory())
    row = store.next_segment_to_aggregate(conn)
    assert row is not None
    assert row["segment_id"] == "seg_0000"


def test_next_segment_none_with_no_coders(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    store.enqueue_segments(conn, _segments(1))
    assert store.next_segment_to_aggregate(conn) is None


def test_next_segment_excludes_already_aggregated(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_two_coders_done(conn, n=1)
    agg_id = store.start_aggregation(conn, "seg_0000")
    assert agg_id is not None
    store.record_aggregation_result(
        conn, aggregation_id=agg_id, segment_id="seg_0000", rows=[]
    )
    assert store.next_segment_to_aggregate(conn) is None


# ---------------------------------------------------------------------------
# aggregate_one
# ---------------------------------------------------------------------------


def test_aggregate_one_persists_codes_and_sources(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_two_coders_done(conn, n=1)

    res = workers.aggregate_one(
        conn, use_mock_embeddings=True, agent_factory=_agg_factory()
    )
    assert res is not None and res["ok"] is True
    assert res["n_merged"] == 1
    assert res["n_retained"] == 1
    assert res["n_in"] == 4  # 2 coders × 2 codes

    # aggregations row marked done
    row = conn.execute(
        "SELECT status, segment_id FROM aggregations"
    ).fetchone()
    assert row["status"] == "done"
    assert row["segment_id"] == "seg_0000"

    rows = conn.execute(
        "SELECT code, quotes_json, source_coders_json FROM aggregated_codes "
        "ORDER BY code"
    ).fetchall()
    assert {r["code"] for r in rows} == {"shared", "unique"}

    by_code = {r["code"]: r for r in rows}
    unique_sources = json.loads(by_code["unique"]["source_coders_json"])
    assert set(unique_sources) == {"alice", "bob"}
    shared_sources = json.loads(by_code["shared"]["source_coders_json"])
    assert set(shared_sources) == {"alice", "bob"}

    quotes = json.loads(by_code["unique"]["quotes_json"])
    assert quotes == [{"quote_id": "seg_0000", "text": "text 0"}]

    # segment status -> reviewing
    seg_status = conn.execute(
        "SELECT status FROM segments WHERE segment_id = 'seg_0000'"
    ).fetchone()["status"]
    assert seg_status == "reviewing"


def test_aggregate_one_returns_none_when_idle(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    assert workers.aggregate_one(conn, agent_factory=_agg_factory()) is None


def test_aggregate_one_failure_records_failed_row(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_two_coders_done(conn, n=1)

    class _Boom:
        def __init__(self, cb): pass
        def aggregate(self, assignments, apply_negotiation=True):
            raise RuntimeError("kaboom")

    res = workers.aggregate_one(
        conn, agent_factory=lambda cb: _Boom(cb)
    )
    assert res is not None and res["ok"] is False
    assert "kaboom" in res["error"]
    row = conn.execute(
        "SELECT status, error FROM aggregations"
    ).fetchone()
    assert row["status"] == "failed"
    assert "kaboom" in row["error"]

    # Retry: clear and re-run with a working agent.
    cleared = store.reset_unfinished_aggregations(conn)
    assert cleared == 1
    res2 = workers.aggregate_one(conn, agent_factory=_agg_factory())
    assert res2 is not None and res2["ok"]


def test_aggregate_one_empty_result_marks_segment_done(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_two_coders_done(conn, n=1)

    class _Empty:
        def __init__(self, cb): pass
        def aggregate(self, assignments, apply_negotiation=True):
            return AggregationResult(merged_codes=[], retained_codes=[])

    res = workers.aggregate_one(conn, agent_factory=lambda cb: _Empty(cb))
    assert res is not None and res["ok"]
    seg_status = conn.execute(
        "SELECT status FROM segments WHERE segment_id = 'seg_0000'"
    ).fetchone()["status"]
    assert seg_status == "done"


def test_drain_aggregate_processes_all_segments(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_two_coders_done(conn, n=3)

    counters = workers.drain_aggregate(
        conn, agent_factory=_agg_factory()
    )
    assert counters == {"done": 3, "failed": 0}
    n_agg = conn.execute(
        "SELECT COUNT(*) AS n FROM aggregations WHERE status='done'"
    ).fetchone()["n"]
    assert n_agg == 3
    n_codes = conn.execute(
        "SELECT COUNT(*) AS n FROM aggregated_codes"
    ).fetchone()["n"]
    assert n_codes == 6  # 3 segments × (1 merged + 1 retained)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_aggregate_runs_against_stub(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "alice", "i"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "bob", "i"]) == 0

    conn = store.connect(db)
    store.enqueue_segments(conn, _segments(2))
    conn.close()

    monkeypatch.setattr(workers, "default_coder_factory", _coder_factory())
    monkeypatch.setattr(
        workers, "default_aggregator_factory", lambda cb: _StubAggregator(cb)
    )

    assert cli.main(
        ["--db", str(db), "code", "alice", "--mock-embeddings"]
    ) == 0
    assert cli.main(
        ["--db", str(db), "code", "bob", "--mock-embeddings"]
    ) == 0
    capsys.readouterr()

    rc = cli.main(["--db", str(db), "update-codebook", "--mock-embeddings"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "done: 2 ok, 0 failed" in out
    assert "merged=" in out and "retained=" in out
