"""Tests for /api/documents/enqueue-random."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from thematic_analysis_inc import db as store  # noqa: E402
from thematic_analysis_inc.web import create_app  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    db = tmp_path / "db.sqlite"
    store.init_db(db).close()
    return TestClient(create_app(db))


def _add_doc(name: str, n_segments: int) -> int:
    doc = store.add_document(name)
    segs = [(None, f"text-{i}", i, i, i) for i in range(n_segments)]
    store.enqueue_segments(doc, segs)
    return doc.document_id


def test_enqueue_random_picks_only_un_enqueued(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store.add_coder("alice")
    already = _add_doc("a.md", 2)
    fresh1 = _add_doc("b.md", 1)
    fresh2 = _add_doc("c.md", 3)
    # Seed an existing queue row on `already` so it's "previously enqueued".
    assert store.coding.enqueue_document(already) >= 1

    r = client.post("/api/documents/enqueue-random", json={})
    assert r.status_code == 200
    body = r.json()
    assert set(body["document_ids"]) == {fresh1, fresh2}
    # 1 + 3 segments × 1 coder = 4 queue rows inserted
    assert body["enqueued"] == 4


def test_enqueue_random_respects_n(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store.add_coder("alice")
    ids = [_add_doc(f"d{i}.md", 1) for i in range(5)]

    r = client.post("/api/documents/enqueue-random?n=2", json={})
    assert r.status_code == 200
    body = r.json()
    assert len(body["document_ids"]) == 2
    assert set(body["document_ids"]).issubset(set(ids))
    assert body["enqueued"] == 2


def test_enqueue_random_empty_when_all_enqueued(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store.add_coder("alice")
    did = _add_doc("only.md", 1)
    store.coding.enqueue_document(did)

    r = client.post("/api/documents/enqueue-random", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["document_ids"] == []
    assert body["enqueued"] == 0
