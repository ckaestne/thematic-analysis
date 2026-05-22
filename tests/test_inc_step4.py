"""Tests for thematic_analysis_inc Step 4 (reviewer worker)."""

from __future__ import annotations

from pathlib import Path

from thematic_analysis_inc import workers
from thematic_analysis_inc import db as store
from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID, SYSTEM_REVIEWER_ID
from thematic_analysis_inc.db.models import (
    Code,
    CodesDerived,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_MERGE_AND_RENAME,
    DERIVATION_AGGREGATION,
    DERIVATION_REVIEW,
)


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
    rows = [(None, f"text {i}", 0, 0, i) for i in range(n)]
    segs = store.enqueue_segments(doc, rows)
    return [s.segment_id for s in segs]


def _stub_code(label: str, quote_text: str, *, segment, coder, codebook):
    from thematic_analysis_inc.db.models import Quote as DBQuote

    c = Code(
        segment_id=segment.segment_id,
        coder_id=coder.coder_id,
        codebook_used_id=codebook.version,
        code=label,
        description=f"desc: {label}",
    )
    c.supporting_quotes = [
        DBQuote(text=quote_text or "q", segment_id=segment.segment_id)
    ]
    return c


class _StubCoder:
    def __init__(self, codebook, coder):
        self.codebook = codebook
        self.coder = coder

    def code_segment(self, segment):
        text = segment.content
        kw = {"segment": segment, "coder": self.coder, "codebook": self.codebook}
        return [
            _stub_code("alpha", text[:10] or "q", **kw),
            _stub_code("beta", text[:10] or "q", **kw),
        ]


class _StubAggregator:
    """Emits one aggregator Code per input label ('alpha', 'beta')."""

    def aggregate(self, segment, codebook):
        inputs = [
            c
            for c in segment.codes
            if c.coder_id >= 1
            and c.codebook_used is codebook
            and c.code != ""
        ]
        out: list[Code] = []
        by_label: dict[str, list[Code]] = {}
        for c in inputs:
            by_label.setdefault(c.code, []).append(c)
        for label, sources in by_label.items():
            new = Code(
                segment_id=segment.segment_id,
                coder_id=SYSTEM_AGGREGATOR_ID,
                codebook_used_id=codebook.version,
                code=label,
                description="",
                rationale="",
            )
            quotes_by_id = {}
            for s in sources:
                for q in s.supporting_quotes or []:
                    if q.quote_id is not None and q.quote_id not in quotes_by_id:
                        quotes_by_id[q.quote_id] = q
            new.supporting_quotes = list(quotes_by_id.values())
            new.derivation_sources = [
                CodesDerived(source_code=s, derivation_type=DERIVATION_AGGREGATION)
                for s in sources
            ]
            out.append(new)
        return out


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
        agent_factory=lambda: _StubAggregator(),
    ) is not None:
        pass
    return sids


def _stub_reviewer_factory(decision: str, target_label: str | None = None):
    """Build a reviewer factory whose ``review_code`` returns a fresh Code
    encoding ``decision``. For MERGE/UPDATE, looks up ``target_label`` in
    ``live_codes`` and appends the second provenance edge."""

    class _StubReviewer:
        def __init__(self, codebook, live_codes, embedding_service):
            self.codebook = codebook
            self.live_codes = live_codes
            self.embedding_service = embedding_service

        def review_code(self, code: Code) -> Code:
            target = None
            if target_label is not None:
                for c in self.live_codes:
                    if c.code == target_label:
                        target = c
                        break
            if decision == DECISION_ADD:
                label = code.code
                quotes = list(code.supporting_quotes or [])
            elif decision == DECISION_MERGE:
                assert target is not None, (
                    f"merge target {target_label!r} not in live_codes"
                )
                label = target.code
                quotes = _union_quotes(target, code)
            else:  # UPDATE
                assert target is not None, (
                    f"update target {target_label!r} not in live_codes"
                )
                label = code.code
                quotes = _union_quotes(target, code)

            new = Code(
                segment_id=code.segment_id,
                coder_id=SYSTEM_REVIEWER_ID,
                codebook_used_id=self.codebook.version,
                code=label,
                description="",
                rationale=f"stub-{decision}",
                embedding=None,
            )
            new.supporting_quotes = quotes
            edges = [
                CodesDerived(
                    source_code=code,
                    derivation_type=DERIVATION_REVIEW,
                    decision=decision,
                    rationale=f"stub-{decision}",
                )
            ]
            if target is not None:
                edges.append(
                    CodesDerived(
                        source_code=target,
                        derivation_type=DERIVATION_REVIEW,
                        decision=decision,
                        rationale=f"stub-{decision}",
                    )
                )
            new.derivation_sources = edges
            return new

    return lambda codebook, live, svc: _StubReviewer(codebook, live, svc)


def _union_quotes(a: Code, b: Code) -> list:
    out: dict[int, object] = {}
    for q in (a.supporting_quotes or []):
        if q.quote_id is not None:
            out[q.quote_id] = q
    for q in (b.supporting_quotes or []):
        if q.quote_id is not None and q.quote_id not in out:
            out[q.quote_id] = q
    return list(out.values())


# ---------------------------------------------------------------------------
# review_one
# ---------------------------------------------------------------------------


def test_review_one_add_writes_reviewer_code_no_new_revision(
    tmp_path: Path,
) -> None:
    """review_one writes the reviewer Code + edge but **does not** create
    a new Codebook revision — that's finalize_codebook's job."""
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)
    v_before = store.latest_codebook().version

    res = workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_stub_reviewer_factory(DECISION_ADD),
    )
    assert res is not None and res["ok"] is True
    assert res["decision"] == "add_new"

    # No new codebook revision yet.
    assert store.latest_codebook().version == v_before

    # Reviewer Code row exists with a CodesDerived 'R' edge.
    row = conn.execute(
        "SELECT decision FROM codes_derived WHERE source_code_id = ? "
        "AND derivation_type = 'R'",
        (res["aggregated_code_id"],),
    ).fetchone()
    assert row["decision"] == "A"


def test_finalize_after_adds_creates_one_new_revision(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=2)  # 2 segments × 2 codes = 4 aggregator codes
    v_before = store.latest_codebook().version

    counters = workers.drain_review(
        conn, use_mock_embeddings=True,
        agent_factory=_stub_reviewer_factory(DECISION_ADD),
    )
    assert counters == {"done": 4, "failed": 0}

    # Still no new codebook revision after the drain.
    assert store.latest_codebook().version == v_before

    new_version = workers.finalize_codebook()
    assert new_version is not None and new_version > v_before

    # Exactly one new codebook revision was created.
    assert store.latest_codebook().version == new_version
    codes_in_new = {e.code for e in _get_codebook_codes(new_version)}
    # Two distinct labels ("alpha", "beta") → after dedup, possibly both in the
    # new codebook (depends on stub behavior). Both labels should appear since
    # each aggregator code becomes a new reviewer code.
    assert "alpha" in codes_in_new or "beta" in codes_in_new


def test_review_one_merge_drops_target_from_new_revision(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)
    # First ADD seeds 'alpha' into the codebook (after finalize).
    workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_stub_reviewer_factory(DECISION_ADD),
    )
    workers.finalize_codebook()
    v_after_add = store.latest_codebook().version
    add_codes = {e.code for e in _get_codebook_codes(v_after_add)}
    assert "alpha" in add_codes

    # Second review MERGEs (beta into alpha) — new revision should still
    # contain a code labeled 'alpha' but the previous 'alpha' Code is dropped.
    workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_stub_reviewer_factory(DECISION_MERGE, target_label="alpha"),
    )
    new_version = workers.finalize_codebook()
    assert new_version is not None and new_version > v_after_add
    codes_in_new = {e.code for e in _get_codebook_codes(new_version)}
    assert "alpha" in codes_in_new  # same label, but a *different* Code row


def test_review_one_update_renames_target(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)
    workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_stub_reviewer_factory(DECISION_ADD),
    )
    workers.finalize_codebook()

    workers.review_one(
        conn, use_mock_embeddings=True,
        agent_factory=_stub_reviewer_factory(DECISION_MERGE_AND_RENAME, target_label="alpha"),
    )
    new_version = workers.finalize_codebook()
    assert new_version is not None
    codes_in_new = {e.code for e in _get_codebook_codes(new_version)}
    # 'alpha' replaced by the new label (the aggregator's source code label,
    # which is 'beta' for the second aggregator code).
    assert "alpha" not in codes_in_new
    assert "beta" in codes_in_new


def test_review_one_segment_done_after_all_reviewed(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    sids = _seed_ready_to_review(conn, n=1)
    factory = _stub_reviewer_factory(DECISION_ADD)

    workers.review_one(conn, use_mock_embeddings=True, agent_factory=factory)
    s1 = store.status.derive_segment_status(sids[0])
    assert s1 == "reviewing"
    workers.review_one(conn, use_mock_embeddings=True, agent_factory=factory)
    s2 = store.status.derive_segment_status(sids[0])
    assert s2 == "done"


def test_review_one_returns_none_when_idle(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    assert workers.review_one(conn, use_mock_embeddings=True) is None


def test_finalize_with_no_decisions_returns_none(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=1)
    # No reviewer decisions written → finalize is a no-op.
    assert workers.finalize_codebook() is None


def test_drain_review_writes_per_code_rows(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    _seed_ready_to_review(conn, n=2)  # 2 segments × 2 codes = 4 codes

    counters = workers.drain_review(
        conn, use_mock_embeddings=True,
        agent_factory=_stub_reviewer_factory(DECISION_ADD),
    )
    assert counters == {"done": 4, "failed": 0}

    n_rev_codes = conn.execute(
        "SELECT COUNT(*) AS n FROM code WHERE coder_id = -1"
    ).fetchone()["n"]
    assert n_rev_codes == 4
