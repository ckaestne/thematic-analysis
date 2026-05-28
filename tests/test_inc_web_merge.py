"""Smoke tests for POST /api/codes/{code_id}/merge.

Regression guard: the MergeIn Pydantic model was previously defined inside
create_app() (closure scope), causing FastAPI to treat the body parameter as a
query parameter and return 422 for any valid JSON body.  These tests confirm
that the endpoint parses the request body correctly.
"""

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


def test_merge_empty_selected_returns_400_not_422(tmp_path: Path) -> None:
    """Body is parsed; empty selected_code_ids triggers a 400, not a 422."""
    client = _client(tmp_path)
    r = client.post("/api/codes/1/merge", json={"selected_code_ids": []})
    assert r.status_code == 400
    assert r.status_code != 422


def test_merge_unknown_code_returns_404_not_422(tmp_path: Path) -> None:
    """Body is parsed; an unknown code_id triggers a 404, not a 422."""
    client = _client(tmp_path)
    r = client.post("/api/codes/99999/merge", json={"selected_code_ids": [1]})
    assert r.status_code == 404
    assert r.status_code != 422
