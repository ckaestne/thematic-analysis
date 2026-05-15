"""Tests for thematic_analysis_inc steps 1–2 (schema, DAL, CLI, code worker)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from thematic_analysis_inc import cli, store, workers
from thematic_analysis_inc.schema import create_schema


def _segments(n: int) -> list[tuple[str, str]]:
    return [(f"seg_{i:04d}", f"text {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------


def test_init_creates_schema_and_v1(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    latest = store.latest_codebook_version(conn)
    assert latest is not None
    assert latest.version == 1
    assert latest.parent_version is None
    assert json.loads(latest.snapshot_json) == {"codes": []}


def test_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    store.init_db(db).close()
    conn = store.init_db(db)
    latest = store.latest_codebook_version(conn)
    assert latest is not None
    assert latest.version == 1


def test_create_schema_idempotent(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "x.sqlite")
    create_schema(conn)
    create_schema(conn)


def test_insert_codebook_version_appends(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    v2 = store.insert_codebook_version(
        conn,
        json.dumps({"codes": [{"code": "x", "quotes": []}]}),
        parent=1,
        created_by="reviewer",
    )
    assert v2 == 2
    latest = store.latest_codebook_version(conn)
    assert latest is not None
    assert latest.version == 2
    assert latest.parent_version == 1


# ---------------------------------------------------------------------------
# Coders + segments
# ---------------------------------------------------------------------------


def test_add_and_remove_coder(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    assert store.add_coder(conn, "c1", "feminist scholar") is True
    assert store.add_coder(conn, "c1", "duplicate") is False  # idempotent
    coders = store.list_coders(conn)
    assert [c.coder_id for c in coders] == ["c1"]
    assert coders[0].identity == "feminist scholar"
    removed, runs_deleted = store.remove_coder(conn, "c1")
    assert removed is True and runs_deleted == 0
    assert store.list_coders(conn) == []


def test_remove_coder_refuses_when_runs_exist(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    store.add_coder(conn, "c1", "x")
    store.enqueue_segments(conn, _segments(1))
    store.start_coder_run(conn, "seg_0000", "c1", 1)
    with pytest.raises(RuntimeError):
        store.remove_coder(conn, "c1")
    # force=True cascades: deletes coder_runs (and any coder_codes) then coder.
    removed, runs_deleted = store.remove_coder(conn, "c1", force=True)
    assert removed is True and runs_deleted == 1
    n_runs = conn.execute("SELECT COUNT(*) AS n FROM coder_runs").fetchone()["n"]
    assert n_runs == 0


def test_enqueue_inserts_segments_only(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    result = store.enqueue_segments(conn, _segments(3))
    assert result.inserted_segments == 3
    assert result.skipped_segments == 0
    n_runs = conn.execute(
        "SELECT COUNT(*) AS n FROM coder_runs"
    ).fetchone()["n"]
    assert n_runs == 0  # no runs created up front


def test_add_document_and_link_segments(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    doc_id = store.add_document(conn, "post.md", b"# hello\n\nworld\n")
    assert doc_id >= 1

    rows = [
        ("seg_b", "second segment text", "Second", doc_id, 1),
        ("seg_a", "first segment text", "First", doc_id, 0),
    ]
    result = store.enqueue_segments(conn, rows)
    assert result.inserted_segments == 2

    fetched = conn.execute(
        "SELECT segment_id, title, document_id, position FROM segments "
        "WHERE document_id = ? ORDER BY position",
        (doc_id,),
    ).fetchall()
    assert [
        (r["segment_id"], r["title"], r["document_id"], r["position"])
        for r in fetched
    ] == [
        ("seg_a", "First", doc_id, 0),
        ("seg_b", "Second", doc_id, 1),
    ]

    doc = conn.execute(
        "SELECT filename, content FROM documents WHERE document_id = ?",
        (doc_id,),
    ).fetchone()
    assert doc["filename"] == "post.md"
    assert bytes(doc["content"]) == b"# hello\n\nworld\n"


def test_enqueue_idempotent_on_segment_id(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    store.enqueue_segments(conn, _segments(2))
    result = store.enqueue_segments(
        conn, _segments(2) + [("seg_0099", "extra")]
    )
    assert result.inserted_segments == 1
    assert result.skipped_segments == 2


def test_segments_to_code_excludes_already_run(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    store.add_coder(conn, "c1", "x")
    store.enqueue_segments(conn, _segments(3))
    todo = store.segments_to_code(conn, "c1")
    assert {r["segment_id"] for r in todo} == {"seg_0000", "seg_0001", "seg_0002"}
    store.start_coder_run(conn, "seg_0000", "c1", 1)
    todo = store.segments_to_code(conn, "c1")
    assert {r["segment_id"] for r in todo} == {"seg_0001", "seg_0002"}


# ---------------------------------------------------------------------------
# Code worker
# ---------------------------------------------------------------------------


@dataclass
class _StubAssignment:
    segment_id: str
    segment_text: str
    codes: list[str] = field(default_factory=list)
    rationales: list[str] = field(default_factory=list)
    is_new_code: list[bool] = field(default_factory=list)


class _StubAgent:
    """Returns canned codes; optionally raises on a configured segment."""

    def __init__(self, codebook, coder, raise_on: str | None = None):
        self.codebook = codebook
        self.coder = coder
        self.raise_on = raise_on

    def code_segment(self, segment_id, text):
        if self.raise_on is not None and segment_id == self.raise_on:
            raise RuntimeError("boom")
        return _StubAssignment(
            segment_id=segment_id,
            segment_text=text,
            codes=[f"{self.coder.coder_id}::{segment_id}::a", "shared"],
            rationales=["because a", "because shared"],
            is_new_code=[True, False],
        )

    async def code_segment_async(self, segment_id, text):
        return self.code_segment(segment_id, text)


def _stub_factory(raise_on: str | None = None):
    def factory(codebook, coder):
        return _StubAgent(codebook, coder, raise_on=raise_on)
    return factory


def test_code_one_persists_codes(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    store.add_coder(conn, "c1", "id1")
    store.enqueue_segments(conn, _segments(2))

    res = workers.code_one(
        conn, "c1", use_mock_embeddings=True, agent_factory=_stub_factory()
    )
    assert res is not None and res["ok"]
    assert res["coder_id"] == "c1"
    assert res["n_codes"] == 2

    rows = conn.execute(
        "SELECT segment_id, status FROM coder_runs ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    codes = conn.execute(
        "SELECT code, position, is_new FROM coder_codes ORDER BY position"
    ).fetchall()
    assert [c["code"] for c in codes][1] == "shared"
    assert codes[0]["is_new"] == 1


def test_code_one_returns_none_when_done(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    store.add_coder(conn, "c1", "id1")
    store.enqueue_segments(conn, _segments(1))
    workers.code_one(conn, "c1", agent_factory=_stub_factory())
    res = workers.code_one(conn, "c1", agent_factory=_stub_factory())
    assert res is None


def test_code_one_failure_records_failed_row(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    store.add_coder(conn, "c1", "id1")
    store.enqueue_segments(conn, _segments(1))
    res = workers.code_one(
        conn, "c1", agent_factory=_stub_factory(raise_on="seg_0000")
    )
    assert res is not None and res["ok"] is False
    assert "boom" in res["error"]
    row = conn.execute(
        "SELECT status, error FROM coder_runs WHERE coder_id='c1'"
    ).fetchone()
    assert row["status"] == "failed"
    assert "boom" in row["error"]

    # retry: clear and re-run
    cleared = store.reset_unfinished_coder_runs(conn, "c1")
    assert cleared == 1
    res2 = workers.code_one(conn, "c1", agent_factory=_stub_factory())
    assert res2 is not None and res2["ok"]


def test_code_one_two_coders_independent(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    conn = store.init_db(db)
    store.add_coder(conn, "c1", "id1")
    store.add_coder(conn, "c2", "id2")
    store.enqueue_segments(conn, _segments(2))
    while workers.code_one(conn, "c1", agent_factory=_stub_factory()) is not None:
        pass
    while workers.code_one(conn, "c2", agent_factory=_stub_factory()) is not None:
        pass
    rows = conn.execute(
        "SELECT coder_id, COUNT(*) AS n FROM coder_runs GROUP BY coder_id "
        "ORDER BY coder_id"
    ).fetchall()
    assert {r["coder_id"]: r["n"] for r in rows} == {"c1": 2, "c2": 2}


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
    assert cli.main(["--db", str(db), "add-coder", "alice", "feminist"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "bob", "marxist"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(db), "list-coders"]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "feminist" in out
    assert "bob" in out and "marxist" in out


def test_cli_rm_coder(tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(["--db", str(db), "add-coder", "alice", "x"]) == 0
    assert cli.main(["--db", str(db), "rm-coder", "alice"]) == 0
    assert cli.main(["--db", str(db), "rm-coder", "ghost"]) == 1


def test_cli_enqueue_jsonl(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    segs_path = tmp_path / "segs.jsonl"
    segs_path.write_text(
        "\n".join(
            json.dumps({"segment_id": f"s{i}", "text": f"t{i}"})
            for i in range(2)
        )
    )
    assert cli.main(["--db", str(db), "init"]) == 0
    assert cli.main(
        ["--db", str(db), "enqueue", "--segments", str(segs_path)]
    ) == 0
    conn = store.connect(db)
    n = conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()["n"]
    assert n == 2


def test_cli_add_document_markdown(tmp_path: Path, capsys) -> None:
    db = tmp_path / "x.sqlite"
    md = tmp_path / "alpha.md"
    md.write_text(
        ("This is a paragraph that has more than twenty words " * 5)
        + "\n\n"
        + ("Another paragraph that also clears the minimum word threshold " * 5)
    )
    assert cli.main(["--db", str(db), "init"]) == 0
    rc = cli.main(["--db", str(db), "add-document", str(md)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha.md" in out and "inserted=" in out


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
    assert cli.main(["--db", str(db), "add-coder", "c1", "voice"]) == 0
    conn = store.connect(db)
    store.enqueue_segments(conn, _segments(3))
    conn.close()

    monkeypatch.setattr(workers, "default_coder_factory", _stub_factory())

    capsys.readouterr()
    rc = cli.main(
        ["--db", str(db), "code", "c1", "--mock-embeddings"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "todo=3" in out
    assert "done: 3 ok, 0 failed" in out

    conn = store.connect(db)
    n_done = conn.execute(
        "SELECT COUNT(*) AS n FROM coder_runs WHERE status='done'"
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
