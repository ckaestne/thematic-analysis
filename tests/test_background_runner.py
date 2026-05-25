"""Tests for the BackgroundRunner worker and its web endpoints."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from thematic_analysis_inc import workers  # noqa: E402
from thematic_analysis_inc.web import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    # Each test gets a fresh process-wide runner instance.
    workers._background_runner = None
    app = create_app(tmp_path / "db.sqlite")
    return TestClient(app)


def test_status_includes_llm_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    r = client.get("/api/background-runner")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["llm_configured"] is False
    assert body["llm_unavailable_reason"]
    assert body["counters"] == {
        "coded": 0,
        "aggregated": 0,
        "reviewed": 0,
        "failed": 0,
    }


def test_start_refused_when_llm_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    r = client.post(
        "/api/background-runner/start", json={"workers": 1}
    )
    assert r.status_code == 400
    assert "LLM" in r.json()["detail"]


def test_start_stop_cycle(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "fake-test-key")
    r = client.post(
        "/api/background-runner/start", json={"workers": 2}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert body["running"] is True
    assert body["workers"] == 2

    # Starting again is a no-op.
    r = client.post(
        "/api/background-runner/start", json={"workers": 4}
    )
    assert r.status_code == 200
    assert r.json()["started"] is False

    r = client.post("/api/background-runner/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["stopped"] is True
    assert body["running"] is False
