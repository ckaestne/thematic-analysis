"""Tests for thematic_analysis_inc Step 3 (aggregator worker) — refactored schema."""

from __future__ import annotations

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
    rows = [(None, f"text {i}", 0, 0, i) for i in range(n)]
    segs = store.enqueue_segments(doc, rows)
    return [s.segment_id for s in segs]


def _stub_code(label: str, quote_text: str):
    from thematic_analysis_inc.db.models import Code, Quote as DBQuote

    c = Code(code=label, description=f"desc: {label}")
    c.supporting_quotes = [DBQuote(text=quote_text or "q")]
    return c


class _StubCoder:
    def __init__(self, codebook, coder):
        self.coder = coder

    def code_segment(self, segment_id, text):
        return [
            _stub_code(f"c{self.coder.coder_id}-only", text[:10] or "q"),
            _stub_code("shared", text[:10] or "q"),
        ]


def _coder_factory():
    def f(cb, coder):
        return _StubCoder(cb, coder)
    return f


class _StubAggregator:
    """Merges all `*-only` codes into 'unique' and retains 'shared'."""

    def __init__(self):
        self.last_assignments = None

    def aggregate(self, coder_codes):
        self.last_assignments = coder_codes
        all_unique = sorted(
            {c.code for codes in coder_codes for c in codes if c.code.endswith("-only")}
        )
        # All codes belong to the same segment.
        seg_id = str(coder_codes[0][0].segment_id)
        sample_quote_text = (
            coder_codes[0][0].supporting_quotes[0].text
            if coder_codes[0][0].supporting_quotes
            else ""
        )
        merged = MergedCode(
            code="unique",
            original_codes=all_unique,
            quotes=[Quote(quote_id=seg_id, text=sample_quote_text)],
            merge_rationale="combined uniques",
        )
        retained = MergedCode(
            code="shared",
            original_codes=["shared"],
            quotes=[Quote(quote_id=seg_id, text=sample_quote_text)],
        )
        return AggregationResult(merged_codes=[merged], retained_codes=[retained])


def _agg_factory():
    def f():
        return _StubAggregator()
    return f


def _seed_two_coders_done(conn, n: int = 1) -> list[int]:
    store.add_coder("id-a")
    store.add_coder("id-b")
    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, n)
    store.coding.enqueue_document(doc.document_id)
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
    store.coding.enqueue_document(doc.document_id)
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
    workers.aggregate_one(conn, agent_factory=_agg_factory())
    assert store.aggregation.next_segment_to_aggregate() is None
    # Aggregator codes exist for this segment.
    assert store.aggregation.segment_has_aggregator_code(sids[0])


# ---------------------------------------------------------------------------
# aggregate_one
# ---------------------------------------------------------------------------


def test_aggregate_one_persists_codes_and_provenance(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    sids = _seed_two_coders_done(conn, n=1)

    res = workers.aggregate_one(conn, agent_factory=_agg_factory())
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
        def aggregate(self, assignments):
            return AggregationResult(merged_codes=[], retained_codes=[])

    res = workers.aggregate_one(conn, agent_factory=_Empty)
    assert res is not None and res["ok"]
    # An empty aggregation result still records a sentinel aggregator row
    # (empty `code`) so the segment is marked aggregated for this
    # codebook version. The sentinel is not reviewable, so the segment is
    # "done".
    assert store.aggregation.segment_has_aggregator_code(sids[0])
    s = store.status.derive_segment_status(sids[0])
    assert s == "done"


class _EmptyCoder:
    def __init__(self, codebook, coder):
        pass

    def code_segment(self, segment_id, text):
        return []


def _empty_coder_factory():
    def f(cb, coder):
        return _EmptyCoder(cb, coder)
    return f


def test_coder_writes_sentinel_when_no_codes(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    store.add_coder("id-a")
    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    store.coding.enqueue_document(doc.document_id)
    res = workers.code_one(
        conn, agent_factory=_empty_coder_factory()
    )
    assert res is not None and res["ok"] is True
    rows = conn.execute(
        "SELECT code FROM code WHERE segment_id = ? AND coder_id >= 1",
        (sids[0],),
    ).fetchall()
    assert [r["code"] for r in rows] == [""]


def test_aggregator_skips_llm_when_all_coders_sentinel(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    store.add_coder("id-a")
    store.add_coder("id-b")
    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    store.coding.enqueue_document(doc.document_id)
    for c in store.list_coders():
        while workers.code_one(
            conn, c.coder_id, agent_factory=_empty_coder_factory()
        ) is not None:
            pass

    called = {"n": 0}

    class _ShouldNotRun:
        def aggregate(self, coder_codes):
            called["n"] += 1
            return AggregationResult(merged_codes=[], retained_codes=[])

    res = workers.aggregate_one(conn, agent_factory=_ShouldNotRun)
    assert res is not None and res["ok"] is True
    assert res.get("empty") is True
    assert called["n"] == 0
    # Aggregator sentinel persisted as a single empty-code row.
    n_sent = conn.execute(
        "SELECT COUNT(*) AS n FROM code "
        "WHERE coder_id = 0 AND segment_id = ? AND code = ''",
        (sids[0],),
    ).fetchone()["n"]
    assert n_sent == 1
    # And the segment is done.
    assert store.status.derive_segment_status(sids[0]) == "done"


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
# Codebook version handling
# ---------------------------------------------------------------------------


def _advance_codebook(conn) -> int:
    """Insert a new codebook version and return its version number."""
    current = store.latest_codebook()
    new_cb = store.insert_codebook_version(parent=current)
    return new_cb.version


def _seed_segment_coded_at_version(conn, seg_id: int, coders: list, cb_version: int) -> None:
    """Enqueue and complete coding for a segment at a specific codebook version."""
    store.coding.enqueue_pairs(
        ((seg_id, c.coder_id) for c in coders),
        codebook_version=cb_version,
    )
    for coder in coders:
        assignment = store.coding.claim_next_assignment(coder)
        if assignment is not None:
            store.coding.record_coding_result(
                assignment,
                [_stub_code(f"code-v{cb_version}-c{coder.coder_id}", "quote")],
            )


def test_next_segment_codebook_finds_old_version(tmp_path: Path) -> None:
    """next_segment_codebook_to_aggregate must find work at older codebook versions."""
    conn = store.init_db(tmp_path / "x.sqlite")
    coder_a = store.add_coder("a")
    coder_b = store.add_coder("b")

    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    sid = sids[0]

    v1 = store.latest_codebook().version

    # Code the segment at v1 with both coders.
    _seed_segment_coded_at_version(conn, sid, [coder_a, coder_b], v1)

    # Advance the codebook — the segment has NOT been coded at v2.
    _advance_codebook(conn)

    # Should still find (segment, v1) even though v2 is the latest.
    result = store.aggregation.next_segment_codebook_to_aggregate()
    assert result is not None
    found_seg, found_version = result
    assert found_seg.segment_id == sid
    assert found_version == v1


def test_next_segment_codebook_skips_version_with_unfinished_queue(tmp_path: Path) -> None:
    """A (segment, version) pair with pending queue entries must not be returned."""
    conn = store.init_db(tmp_path / "x.sqlite")
    coder_a = store.add_coder("a")
    coder_b = store.add_coder("b")

    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    sid = sids[0]

    v1 = store.latest_codebook().version

    # Only coder_a finishes; coder_b's entry stays pending.
    store.coding.enqueue_pairs(
        ((sid, coder_a.coder_id), (sid, coder_b.coder_id)),
        codebook_version=v1,
    )
    a_assign = store.coding.claim_next_assignment(coder_a)
    store.coding.record_coding_result(a_assign, [_stub_code("x", "q")])

    assert store.aggregation.next_segment_codebook_to_aggregate() is None


def test_aggregate_one_uses_codebook_version_from_db(tmp_path: Path) -> None:
    """aggregate_one must aggregate at the discovered codebook version, not latest."""
    conn = store.init_db(tmp_path / "x.sqlite")
    coder_a = store.add_coder("a")
    coder_b = store.add_coder("b")

    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    sid = sids[0]

    v1 = store.latest_codebook().version
    _seed_segment_coded_at_version(conn, sid, [coder_a, coder_b], v1)

    # Advance codebook — aggregate_one must still process the v1 work.
    _advance_codebook(conn)

    res = workers.aggregate_one(conn, agent_factory=_agg_factory())
    assert res is not None and res["ok"] is True
    assert res["codebook_version"] == v1

    # Aggregator code should be at v1, not v2.
    rows = conn.execute(
        "SELECT codebook_used_id FROM code WHERE coder_id = 0 AND segment_id = ?",
        (sid,),
    ).fetchall()
    assert all(r["codebook_used_id"] == v1 for r in rows)


def test_unaggregated_codebook_versions_for_segment(tmp_path: Path) -> None:
    """unaggregated_codebook_versions_for_segment returns all pending versions."""
    conn = store.init_db(tmp_path / "x.sqlite")
    coder_a = store.add_coder("a")

    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    sid = sids[0]

    v1 = store.latest_codebook().version
    _seed_segment_coded_at_version(conn, sid, [coder_a], v1)

    v2 = _advance_codebook(conn)
    _seed_segment_coded_at_version(conn, sid, [coder_a], v2)

    versions = store.aggregation.unaggregated_codebook_versions_for_segment(sid)
    assert versions == [v1, v2]

    # After aggregating v1, only v2 should remain.
    workers.aggregate_one(conn, agent_factory=_agg_factory())
    versions = store.aggregation.unaggregated_codebook_versions_for_segment(sid)
    assert versions == [v2]


def test_aggregate_segment_processes_all_versions(tmp_path: Path) -> None:
    """aggregate_segment must aggregate every codebook version for the segment."""
    conn = store.init_db(tmp_path / "x.sqlite")
    coder_a = store.add_coder("a")

    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    sid = sids[0]

    v1 = store.latest_codebook().version
    _seed_segment_coded_at_version(conn, sid, [coder_a], v1)

    v2 = _advance_codebook(conn)
    _seed_segment_coded_at_version(conn, sid, [coder_a], v2)

    results = workers.aggregate_segment(conn, sid, agent_factory=_agg_factory())
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    processed_versions = {r["codebook_version"] for r in results}
    assert processed_versions == {v1, v2}

    # No more versions to aggregate.
    assert store.aggregation.unaggregated_codebook_versions_for_segment(sid) == []


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

    assert cli.main(
        ["--db", str(db), "enqueue", "--document", str(doc.document_id)]
    ) == 0

    monkeypatch.setattr(workers, "default_coder_factory", _coder_factory())
    monkeypatch.setattr(
        workers, "default_aggregator_factory", lambda: _StubAggregator()
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

    assert cli.main(["--db", str(db), "code", "--mock-embeddings"]) == 0
    capsys.readouterr()

    rc = cli.main(["--db", str(db), "update-codebook", "--mock-embeddings"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "merged=" in out and "retained=" in out
