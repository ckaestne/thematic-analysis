"""Tests for POST /api/documents (document upload endpoint)."""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from thematic_analysis_inc import db as store  # noqa: E402
from thematic_analysis_inc.web import create_app  # noqa: E402


def _make_client(tmp_path: Path) -> TestClient:
    db = tmp_path / "db.sqlite"
    store.init_db(db).close()
    return TestClient(create_app(db))


# A block of text with clearly distinct paragraphs (>50 words each).
_PARA_A = " ".join(["word"] * 60)
_PARA_B = " ".join(["other"] * 60)
_TWO_PARA_TEXT = f"{_PARA_A}\n\n{_PARA_B}"


def test_upload_creates_document_and_segments(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    r = client.post(
        "/api/documents",
        files={"file": ("interview.txt", _TWO_PARA_TEXT.encode(), "text/plain")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "interview.txt"
    assert body["segments_total"] == 2
    assert isinstance(body["document_id"], int)

    # Document appears in list
    list_r = client.get("/api/documents")
    assert any(d["filename"] == "interview.txt" for d in list_r.json()["items"])


def test_upload_rejects_duplicate_filename(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    payload = {"file": ("dup.txt", _TWO_PARA_TEXT.encode(), "text/plain")}
    assert client.post("/api/documents", files=payload).status_code == 200
    r2 = client.post("/api/documents", files=payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


def test_upload_rejects_empty_file(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    r = client.post(
        "/api/documents",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert r.status_code == 400


def test_upload_rejects_non_utf8(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    r = client.post(
        "/api/documents",
        files={"file": ("binary.bin", bytes(range(256)), "application/octet-stream")},
    )
    assert r.status_code == 400


def test_upload_rejects_unsegmentable_text(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    r = client.post(
        "/api/documents",
        files={"file": ("tiny.txt", b"too short", "text/plain")},
    )
    assert r.status_code == 422
