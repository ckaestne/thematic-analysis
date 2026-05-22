"""Smoke tests for the /api/status payload.

The bundled SPA reads specific field names (some of which are pre-refactor
legacy names kept as aliases). Missing fields cause the React overview
page to crash. These tests assert every field the SPA references is in
the response.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from thematic_analysis_inc.web import create_app  # noqa: E402


# Fields the bundled SPA (web_static/assets/index-*.js) reads from
# /api/status.stage1. Verified by `grep -oE '\.(field)' assets/*.js`.
SPA_STATUS_STAGE1_FIELDS = {
    "segments_total",
    "segments_by_status",
    "codebook_version",
    "codebook_codes",
    "coder_runs_by_status",
    "aggregations_total",
    "aggregations_by_status",
    "review_decisions_total",
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(tmp_path / "db.sqlite")
    return TestClient(app)


def test_status_payload_has_every_field_the_spa_reads(
    client: TestClient,
) -> None:
    r = client.get("/api/status")
    assert r.status_code == 200
    payload = r.json()
    assert "stage1" in payload
    stage1 = payload["stage1"]
    missing = SPA_STATUS_STAGE1_FIELDS - set(stage1)
    assert not missing, (
        f"SPA-required fields missing from /api/status.stage1: {missing}. "
        "This will crash the React overview page."
    )


def test_status_legacy_aliases_match_new_fields(
    client: TestClient,
) -> None:
    """The legacy aliases must report the same data as the new fields."""
    payload = client.get("/api/status").json()["stage1"]
    assert payload["coder_runs_total"] == payload["coding_queue_total"]
    assert (
        payload["coder_runs_by_status"]
        == payload["coding_queue_by_status"]
    )
    assert (
        payload["aggregations_total"]
        == payload["aggregator_segments_total"]
    )
    assert payload["review_decisions_total"] == sum(
        payload["review_decisions_by_kind"].values()
    )


def test_status_endpoints_reachable(client: TestClient) -> None:
    """Endpoints the SPA hits on initial load must all return 200."""
    for ep in [
        "/api/status",
        "/api/codebook/versions",
        "/api/coders",
        "/api/documents",
        "/api/research-context",
    ]:
        r = client.get(ep)
        assert r.status_code == 200, (
            f"{ep} returned {r.status_code}: {r.text[:200]}"
        )
