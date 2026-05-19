"""Tests for thematic_analysis_inc Step 1 (schema, DAL, code worker).

Updated for the refactored schema:
- INTEGER segment_ids (auto-assigned)
- INTEGER coder_ids (>= 1 for real coders, 0 = aggregator, -1 = reviewer)
- coding_queue replaces coder_runs (status derived from claimed_at/finished_at/error)
- codes table replaces coder_codes; quotes / codes_supporting_quotes
- codebook_versions has no snapshot_json
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thematic_analysis_inc import cli, workers
from thematic_analysis_inc import db as store
from thematic_analysis_inc.db.models import Code, Quote
from thematic_analysis_inc.db.schema import create_schema


def _add_segments(conn, doc, n: int):
    """``doc`` may be a Document object or a document_id."""
    if isinstance(doc, int):
        # legacy: look it up
        from thematic_analysis_inc.db.connection import session
        from thematic_analysis_inc.db.models import Document
        with session() as s:
            doc = s.get(Document, doc)
            s.expunge(doc)
    rows = [(f"text {i}", 0, 0, i) for i in range(n)]
    segs = store.enqueue_segments(doc, rows)
    return [s.segment_id for s in segs]


def _seed_document(conn):
    return store.add_document("doc.md")


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------


def test_init_creates_schema_and_v1(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    latest = store.latest_codebook()
    assert latest is not None
    assert latest.version == 1
    assert latest.parent_version is None
    # Empty codebook serialises to {"codes": []}
    snap = json.loads(store.codebook_to_json_for_version(1))
    assert snap == {"codes": []}


def test_init_seeds_system_coders(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    store.init_db(db)
    agg = store.get_coder(0)
    rev = store.get_coder(-1)
    assert agg is not None and "aggregator" in agg.identity
    assert rev is not None and "reviewer" in rev.identity
    # list_coders only returns real coders.
    assert store.list_coders() == []


def test_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    store.init_db(db).close()
    conn = store.init_db(db)
    latest = store.latest_codebook()
    assert latest is not None
    assert latest.version == 1


def test_create_schema_idempotent(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "x.sqlite")
    create_schema(conn)
    create_schema(conn)


def test_insert_codebook_version_appends(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    store.init_db(db)
    parent = store.latest_codebook()
    cb2 = store.insert_codebook_version(parent=parent)
    assert cb2.version == 2
    latest = store.latest_codebook()
    assert latest is not None
    assert latest.version == 2
    assert latest.parent_version == 1


# ---------------------------------------------------------------------------
# Coders + segments
# ---------------------------------------------------------------------------


def test_add_coder_assigns_increasing_ids(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    c1 = store.add_coder("feminist scholar")
    c2 = store.add_coder("marxist")
    assert c1.coder_id == 1 and c2.coder_id == 2
    assert [c.coder_id for c in store.list_coders()] == [1, 2]


def test_remove_coder_refuses_when_queue_rows_exist(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    c = store.add_coder("x")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 1)
    store.coding.enqueue_document(doc.document_id)
    with pytest.raises(RuntimeError):
        store.cascades.delete_coder_cascade(c)
    removed, queue_deleted = store.cascades.delete_coder_cascade(
        c, force=True
    )
    assert removed is True and queue_deleted == 1
    n_queue = conn.execute(
        "SELECT COUNT(*) AS n FROM coding_queue"
    ).fetchone()["n"]
    assert n_queue == 0


def test_enqueue_inserts_segments_only(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 3)
    n_queue = conn.execute(
        "SELECT COUNT(*) AS n FROM coding_queue"
    ).fetchone()["n"]
    assert n_queue == 0  # no queue rows until enqueue_document is called


def test_add_document_and_link_segments(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    doc = store.add_document("post.md")
    assert doc.document_id >= 1
    sids = _add_segments(conn, doc, 2)
    assert len(sids) == 2
    found = store.find_document_by_filename("post.md")
    assert found is not None and found.filename == "post.md"


def test_enqueue_document_pairs_segments_and_coders(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    store.add_coder("i")
    store.add_coder("i")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 3)
    n = store.coding.enqueue_document(doc.document_id)
    assert n == 6  # 3 segments x 2 real coders
    # Re-enqueueing at the same codebook+RC is a no-op.
    assert store.coding.enqueue_document(doc.document_id) == 0


def test_enqueue_segment_default_all_coders(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    store.add_coder("a")
    store.add_coder("b")
    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    n = store.coding.enqueue_segment(sids[0])
    assert n == 2


def test_enqueue_creates_new_row_when_codebook_changes(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    store.add_coder("a")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 1)
    assert store.coding.enqueue_document(doc.document_id) == 1
    # Bump the codebook revision and re-enqueue → fresh row.
    store.insert_codebook_version(parent=store.latest_codebook())
    assert store.coding.enqueue_document(doc.document_id) == 1
    n_queue = conn.execute(
        "SELECT COUNT(*) AS n FROM coding_queue"
    ).fetchone()["n"]
    assert n_queue == 2


# ---------------------------------------------------------------------------
# Code worker
# ---------------------------------------------------------------------------


def _stub_code(label: str, quote_text: str) -> Code:
    c = Code(code=label, description=f"desc: {label}")
    c.supporting_quotes = [Quote(text=quote_text)]
    return c


class _StubAgent:
    """Returns canned codes; optionally raises on a configured segment."""

    def __init__(self, codebook, coder, raise_on: str | None = None):
        self.codebook = codebook
        self.coder = coder
        self.raise_on = raise_on

    def code_segment(self, segment_id, text):
        if self.raise_on is not None and str(segment_id) == self.raise_on:
            raise RuntimeError("boom")
        return [
            _stub_code(f"{self.coder.coder_id}::{segment_id}::a", text[:10] or "q"),
            _stub_code("shared", text[:10] or "q"),
        ]

    async def code_segment_async(self, segment_id, text):
        return self.code_segment(segment_id, text)


def _stub_factory(raise_on: str | None = None):
    def factory(codebook, coder):
        return _StubAgent(codebook, coder, raise_on=raise_on)
    return factory


def test_code_one_persists_codes(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    c = store.add_coder("id1")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 2)
    store.coding.enqueue_document(doc.document_id)

    res = workers.code_one(
        conn, c.coder_id, use_mock_embeddings=True,
        agent_factory=_stub_factory(),
    )
    assert res is not None and res["ok"]
    assert res["coder_id"] == c.coder_id
    assert res["n_codes"] == 2

    # Queue row done.
    row = conn.execute(
        "SELECT claimed_at, finished_at, error FROM coding_queue "
        "WHERE coder_id = ? AND finished_at IS NOT NULL",
        (c.coder_id,),
    ).fetchone()
    assert row is not None and row["error"] is None

    codes = conn.execute(
        "SELECT code, rationale FROM code WHERE coder_id = ? ORDER BY code_id",
        (c.coder_id,),
    ).fetchall()
    assert {row["code"] for row in codes} >= {"shared"}


def test_code_one_returns_none_when_done(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    c = store.add_coder("id1")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 1)
    store.coding.enqueue_document(doc.document_id)
    workers.code_one(conn, c.coder_id, agent_factory=_stub_factory())
    res = workers.code_one(conn, c.coder_id, agent_factory=_stub_factory())
    assert res is None


def test_code_one_failure_records_error_in_queue(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    c = store.add_coder("id1")
    doc = _seed_document(conn)
    sids = _add_segments(conn, doc, 1)
    store.coding.enqueue_document(doc.document_id)
    res = workers.code_one(
        conn, c.coder_id,
        agent_factory=_stub_factory(raise_on=str(sids[0])),
    )
    assert res is not None and res["ok"] is False
    assert "boom" in res["error"]
    row = conn.execute(
        "SELECT error FROM coding_queue WHERE coder_id = ?", (c.coder_id,)
    ).fetchone()
    assert row is not None and "boom" in row["error"]

    # Reset failed → retry.
    cleared = store.coding.reset_failed_assignments(store.get_coder(c.coder_id))
    assert cleared == 1
    res2 = workers.code_one(conn, c.coder_id, agent_factory=_stub_factory())
    assert res2 is not None and res2["ok"]


def test_code_one_two_coders_independent(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "x.sqlite")
    a = store.add_coder("id1")
    b = store.add_coder("id2")
    doc = _seed_document(conn)
    _add_segments(conn, doc, 2)
    store.coding.enqueue_document(doc.document_id)
    while workers.code_one(conn, a.coder_id, agent_factory=_stub_factory()) is not None:
        pass
    while workers.code_one(conn, b.coder_id, agent_factory=_stub_factory()) is not None:
        pass
    rows = conn.execute(
        "SELECT coder_id, COUNT(*) AS n FROM coding_queue "
        "WHERE finished_at IS NOT NULL GROUP BY coder_id ORDER BY coder_id"
    ).fetchall()
    assert {r["coder_id"]: r["n"] for r in rows} == {
        a.coder_id: 2, b.coder_id: 2
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_init_status(tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(["--db", str(db), "status"]) == 0
    out = capsys.readouterr().out
    assert "initialized" in out and "segments:" in out and "v=1" in out


def test_cli_add_and_list_coders(tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "feminist"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "marxist"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(db), "list-coders"]) == 0
    out = capsys.readouterr().out
    assert "feminist" in out
    assert "marxist" in out


def test_cli_rm_coder(tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "x"]) == 0
    assert cli.main(["--db", str(db), "rm-coder", "1"]) == 0
    assert cli.main(["--db", str(db), "rm-coder", "999"]) == 1


def test_cli_add_document_markdown(tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    md = tmp_path / "alpha.md"
    md.write_text(
        ("This is a paragraph that has more than twenty words " * 5)
        + "\n\n"
        + ("Another paragraph that also clears the minimum word threshold " * 5)
    )
    assert cli.main(["--db", str(db), "init"]) == 0
    rc = cli.main(
        ["--db", str(db), "add-document", "--segmentation", "paragraph",
         str(md)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha.md" in out and "inserted=" in out
    assert "created_document_id\t1" in out


def test_cli_add_document_skips_existing_filename(tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    md = tmp_path / "alpha.md"
    md.write_text(
        ("This is a paragraph that has more than twenty words " * 5)
        + "\n\n"
        + ("Another paragraph that also clears the minimum word threshold " * 5)
    )
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(
        ["--db", str(db), "add-document", "--segmentation", "paragraph",
         str(md)]
    ) == 0
    capsys.readouterr()

    rc = cli.main(
        ["--db", str(db), "add-document", "--segmentation", "paragraph",
         str(md)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "already exists" in out

    conn = store.connect(db)
    n_docs = conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"]
    assert n_docs == 1


def test_cli_list_documents(tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    first = tmp_path / "alpha.md"
    second = tmp_path / "beta.md"
    first.write_text(
        ("This is a paragraph that has more than twenty words " * 5)
        + "\n\n"
        + ("Another paragraph that also clears the minimum word threshold " * 5)
    )
    second.write_text(" ".join(["word"] * 80))

    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(
        ["--db", str(db), "add-document", "--segmentation", "paragraph", str(first)]
    ) == 0
    capsys.readouterr()
    assert cli.main(
        ["--db", str(db), "add-document", "--segmentation", "fixed", str(second)]
    ) == 0
    capsys.readouterr()

    rc = cli.main(["--db", str(db), "list-documents"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["2\tbeta.md\t1", "1\talpha.md\t2"]


def test_cli_add_document_missing_file(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    rc = cli.main(
        ["--db", str(db), "add-document", str(tmp_path / "nope.md")]
    )
    assert rc == 1


def test_cli_code_runs_against_stub(tmp_path: Path, capsys, monkeypatch) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "voice"]) == 0
    conn = store.connect(db)
    doc = _seed_document(conn)
    _add_segments(conn, doc, 3)
    conn.close()

    # Enqueue all segments for the registered coder via the CLI.
    assert cli.main(
        ["--db", str(db), "enqueue", "--document", str(doc.document_id)]
    ) == 0

    monkeypatch.setattr(workers, "default_coder_factory", _stub_factory())

    capsys.readouterr()
    rc = cli.main(
        ["--db", str(db), "code", "1", "--mock-embeddings"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "todo=3" in out
    assert "done: 3 ok, 0 failed" in out

    conn = store.connect(db)
    n_done = conn.execute(
        "SELECT COUNT(*) AS n FROM coding_queue WHERE finished_at IS NOT NULL"
    ).fetchone()["n"]
    assert n_done == 3


def test_cli_code_unknown_coder(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    rc = cli.main(["--db", str(db), "code", "nope"])
    assert rc == 1


def test_cli_export_codebook(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    out = tmp_path / "cb.json"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(
        ["--db", str(db), "export-codebook", "-o", str(out)]
    ) == 0
    assert json.loads(out.read_text()) == {"codes": []}
