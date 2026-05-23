"""Tests for the POST /api/documents (document upload) endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from thematic_analysis_inc import db as store  # noqa: E402
from thematic_analysis_inc.web import create_app  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    db = tmp_path / "db.sqlite"
    store.init_db(db).close()
    return TestClient(create_app(db))


_LONG_TEXT = "\n\n".join(
    [
        (
            "This is the first paragraph of the test document. "
            "It contains enough words to satisfy the minimum word threshold for segmentation."
        ),
        (
            "This is the second paragraph of the test document. "
            "It also contains a sufficient number of words so that it is kept as a separate segment."
        ),
        (
            "And here is a third paragraph with even more content. "
            "This one also has enough words to be retained as its own segment by the segmenter."
        ),
    ]
)

_SHORT_TEXT = "Too short."


def test_upload_creates_document(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.post(
        "/api/documents",
        files={"file": ("my_doc.txt", _LONG_TEXT.encode(), "text/plain")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["filename"] == "my_doc.txt"
    assert body["segments_inserted"] == 3
    assert body["document_id"] >= 1


def test_upload_document_appears_in_list(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post(
        "/api/documents",
        files={"file": ("doc.txt", _LONG_TEXT.encode(), "text/plain")},
    )
    res = client.get("/api/documents")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["filename"] == "doc.txt"
    assert items[0]["segments_total"] == 3


def test_upload_rejects_duplicate_filename(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post(
        "/api/documents",
        files={"file": ("dup.txt", _LONG_TEXT.encode(), "text/plain")},
    )
    res = client.post(
        "/api/documents",
        files={"file": ("dup.txt", _LONG_TEXT.encode(), "text/plain")},
    )
    assert res.status_code == 409


def test_upload_rejects_empty_file(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.post(
        "/api/documents",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert res.status_code == 400


def test_upload_rejects_non_utf8(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.post(
        "/api/documents",
        files={"file": ("binary.bin", b"\xff\xfe bad bytes", "application/octet-stream")},
    )
    assert res.status_code == 400


def test_upload_rejects_too_short_for_segments(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.post(
        "/api/documents",
        files={"file": ("short.txt", _SHORT_TEXT.encode(), "text/plain")},
    )
    assert res.status_code == 422
