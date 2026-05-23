"""Tests for the /api/segments/{id} payload's coder_codes section.

Covers the "no coders yet" vs "coder finished with zero codes"
distinction and the inclusion of the codebook version on each coder
block.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from thematic_analysis_inc import db as store  # noqa: E402
from thematic_analysis_inc.db.connection import session  # noqa: E402
from thematic_analysis_inc.db.models import CodingQueueEntry  # noqa: E402
from thematic_analysis_inc.web import create_app  # noqa: E402


def _seed(tmp_path: Path):
    db = tmp_path / "db.sqlite"
    store.init_db(db).close()
    app = create_app(db)
    client = TestClient(app)
    doc = store.add_document("doc.md")
    coder = store.add_coder("alice")
    segs = store.enqueue_segments(doc, [(None, "hello world", 0, 0, 0)])
    return client, coder, segs[0].segment_id


def test_segment_payload_omits_coders_without_queue_entry(tmp_path: Path) -> None:
    client, _coder, sid = _seed(tmp_path)
    r = client.get(f"/api/segments/{sid}")
    assert r.status_code == 200
    assert r.json()["coder_codes"] == []


def test_segment_payload_includes_finished_coder_with_zero_codes(
    tmp_path: Path,
) -> None:
    client, coder, sid = _seed(tmp_path)
    assert store.coding.enqueue_segment(sid) == 1
    # Mark the queue entry done without inserting any Code rows.
    with session() as s:
        from sqlmodel import select

        q = s.exec(select(CodingQueueEntry)).one()
        q.claimed_at = datetime.now(timezone.utc)
        q.finished_at = datetime.now(timezone.utc)
        s.add(q)
        s.commit()
        codebook_v = q.codebook_used_id

    r = client.get(f"/api/segments/{sid}")
    assert r.status_code == 200
    blocks = r.json()["coder_codes"]
    assert len(blocks) == 1
    block = blocks[0]
    assert block["coder_id"] == coder.coder_id
    assert block["status"] == "done"
    assert block["codes"] == []
    assert block["codebook_version"] == codebook_v
    assert block["finished_at"] is not None


def test_segment_payload_includes_document_filename(tmp_path: Path) -> None:
    client, _coder, sid = _seed(tmp_path)
    r = client.get(f"/api/segments/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["document_filename"] == "doc.md"


def test_segment_payload_pending_coder_listed(tmp_path: Path) -> None:
    client, coder, sid = _seed(tmp_path)
    assert store.coding.enqueue_segment(sid) == 1
    r = client.get(f"/api/segments/{sid}")
    assert r.status_code == 200
    blocks = r.json()["coder_codes"]
    assert len(blocks) == 1
    assert blocks[0]["status"] == "pending"
    assert blocks[0]["codes"] == []
    assert blocks[0]["codebook_version"] >= 1
