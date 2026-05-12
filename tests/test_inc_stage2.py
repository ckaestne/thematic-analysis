"""Tests for the incremental Stage 2 pipeline (theme coding + aggregation)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thematic_analysis.agents.theme_coder import Theme, ThemeResult
from thematic_analysis.agents.theme_aggregator import (
    MergedTheme,
    ThemeAggregationResult,
)
from thematic_analysis.codebook import Codebook

from thematic_analysis_inc import cli_stage2, store, workers
from thematic_analysis_inc.schema import create_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init(tmp_path: Path):
    """Initialise a DB with stage-1 schema + v1 codebook."""
    db = tmp_path / "test.sqlite"
    return store.init_db(db), db


def _stub_theme_result(themes: list[tuple[str, str, list[str]]]) -> ThemeResult:
    """Build a ThemeResult from (name, description, codes) tuples."""
    return ThemeResult(
        themes=[
            Theme(name=n, description=d, codes=c) for n, d, c in themes
        ]
    )


class _StubThemeCoder:
    """Returns a fixed ThemeResult regardless of codebook."""

    def __init__(self, theme_result: ThemeResult):
        self._result = theme_result

    def develop_themes(self) -> ThemeResult:
        return self._result


class _StubThemeAggregator:
    """Returns a fixed ThemeAggregationResult."""

    def __init__(self, result: ThemeAggregationResult):
        self._result = result

    def aggregate(self, theme_results: list[ThemeResult]) -> ThemeAggregationResult:
        return self._result


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_stage2_schema_idempotent(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "x.sqlite")
    create_schema(conn)
    create_schema(conn)  # second call must not raise


def test_stage2_tables_exist(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"theme_coders", "theme_coder_runs", "theme_aggregations",
            "theme_aggregation_inputs"}.issubset(tables)


# ---------------------------------------------------------------------------
# ThemeCoder management
# ---------------------------------------------------------------------------


def test_add_and_remove_theme_coder(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    assert store.add_theme_coder(conn, "tc1", "critical theorist") is True
    assert store.add_theme_coder(conn, "tc1", "duplicate") is False
    coders = store.list_theme_coders(conn)
    assert [c.theme_coder_id for c in coders] == ["tc1"]
    assert coders[0].identity == "critical theorist"
    removed, runs = store.remove_theme_coder(conn, "tc1")
    assert removed is True and runs == 0
    assert store.list_theme_coders(conn) == []


def test_remove_theme_coder_refuses_if_runs_exist(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "identity")
    store.start_theme_coder_run(conn, "tc1", 1)
    with pytest.raises(RuntimeError, match="force"):
        store.remove_theme_coder(conn, "tc1")


def test_remove_theme_coder_force(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "identity")
    store.start_theme_coder_run(conn, "tc1", 1)
    removed, runs = store.remove_theme_coder(conn, "tc1", force=True)
    assert removed is True and runs == 1


# ---------------------------------------------------------------------------
# theme_code_one — success and failure paths
# ---------------------------------------------------------------------------


def _theme_coder_factory(result: ThemeResult):
    def factory(codebook, theme_coder):
        return _StubThemeCoder(result)
    return factory


def test_theme_code_one_success(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "analyst")

    tr = _stub_theme_result([
        ("Identity", "About identity", ["code-a", "code-b"]),
        ("Power", "About power", ["code-c"]),
    ])

    res = workers.theme_code_one(
        conn, "tc1", codebook_version=1,
        agent_factory=_theme_coder_factory(tr),
    )
    assert res is not None
    assert res["ok"] is True
    assert res["theme_coder_id"] == "tc1"
    assert res["n_themes"] == 2

    # Calling again returns None (already done)
    res2 = workers.theme_code_one(
        conn, "tc1", codebook_version=1,
        agent_factory=_theme_coder_factory(tr),
    )
    assert res2 is None


def test_theme_code_one_failure(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "analyst")

    def bad_factory(codebook, theme_coder):
        raise RuntimeError("LLM exploded")

    res = workers.theme_code_one(conn, "tc1", codebook_version=1, agent_factory=bad_factory)
    assert res is not None
    assert res["ok"] is False
    assert "LLM exploded" in res["error"]

    run = conn.execute(
        "SELECT status, error FROM theme_coder_runs WHERE id = ?", (res["run_id"],)
    ).fetchone()
    assert run["status"] == "failed"
    assert "LLM exploded" in run["error"]


def test_theme_code_one_unknown_coder(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    with pytest.raises(ValueError, match="unknown theme_coder_id"):
        workers.theme_code_one(conn, "ghost", codebook_version=1)


# ---------------------------------------------------------------------------
# drain_theme_code_async — multiple coders
# ---------------------------------------------------------------------------


def test_drain_theme_code_two_coders(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "analyst-1")
    store.add_theme_coder(conn, "tc2", "analyst-2")

    tr = _stub_theme_result([("T1", "desc", ["c1", "c2"])])

    import asyncio
    counters = asyncio.run(
        workers.drain_theme_code_async(
            conn, codebook_version=1,
            workers=2,
            agent_factory=_theme_coder_factory(tr),
        )
    )
    assert counters["done"] == 2
    assert counters["failed"] == 0

    # Both runs persisted as done
    done_runs = conn.execute(
        "SELECT COUNT(*) AS n FROM theme_coder_runs WHERE status = 'done'"
    ).fetchone()["n"]
    assert done_runs == 2


def test_drain_theme_code_respects_limit(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    for i in range(4):
        store.add_theme_coder(conn, f"tc{i}", f"analyst-{i}")

    tr = _stub_theme_result([("T1", "desc", ["c1"])])

    import asyncio
    counters = asyncio.run(
        workers.drain_theme_code_async(
            conn, codebook_version=1,
            limit=2,
            agent_factory=_theme_coder_factory(tr),
        )
    )
    assert counters["done"] == 2


# ---------------------------------------------------------------------------
# all_theme_coders_done
# ---------------------------------------------------------------------------


def test_all_theme_coders_done_false_when_no_coders(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    assert store.all_theme_coders_done(conn, 1) is False


def test_all_theme_coders_done_false_while_pending(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    store.add_theme_coder(conn, "tc2", "b")
    store.start_theme_coder_run(conn, "tc1", 1)
    store.record_theme_coder_result(conn, 1, '{"themes":[]}')
    assert store.all_theme_coders_done(conn, 1) is False  # tc2 still pending


def test_all_theme_coders_done_true_when_all_done(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    store.add_theme_coder(conn, "tc2", "b")

    tr = _stub_theme_result([("T1", "desc", ["c1"])])
    import asyncio
    asyncio.run(
        workers.drain_theme_code_async(
            conn, 1, agent_factory=_theme_coder_factory(tr)
        )
    )
    assert store.all_theme_coders_done(conn, 1) is True


# ---------------------------------------------------------------------------
# theme_aggregate_one
# ---------------------------------------------------------------------------


def _agg_factory(result: ThemeAggregationResult):
    def factory():
        return _StubThemeAggregator(result)
    return factory


def test_theme_aggregate_one_returns_none_when_not_ready(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    # No runs yet
    res = workers.theme_aggregate_one(conn, codebook_version=1)
    assert res is None


def test_theme_aggregate_one_success(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    store.add_theme_coder(conn, "tc2", "b")

    tr = _stub_theme_result([("T1", "desc", ["c1", "c2"])])
    import asyncio
    asyncio.run(
        workers.drain_theme_code_async(
            conn, 1, agent_factory=_theme_coder_factory(tr)
        )
    )

    agg_result = ThemeAggregationResult(themes=[
        MergedTheme(
            name="Merged T1",
            description="merged desc",
            original_themes=["T1"],
            codes=["c1", "c2"],
        )
    ])
    res = workers.theme_aggregate_one(
        conn, codebook_version=1, agent_factory=_agg_factory(agg_result)
    )
    assert res is not None
    assert res["ok"] is True
    assert res["n_themes"] == 1
    assert res["n_input_results"] == 2

    # Result persisted in DB
    row = store.latest_theme_aggregation(conn, 1)
    assert row is not None
    assert row["status"] == "done"
    themes = json.loads(row["result_json"])["themes"]
    assert themes[0]["name"] == "Merged T1"

    # Inputs linked
    inputs = conn.execute(
        "SELECT COUNT(*) AS n FROM theme_aggregation_inputs "
        "WHERE theme_aggregation_id = ?", (row["id"],)
    ).fetchone()["n"]
    assert inputs == 2


def test_theme_aggregate_one_idempotent(tmp_path: Path) -> None:
    """Second call returns None because aggregation already exists."""
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    tr = _stub_theme_result([("T1", "desc", ["c1"])])
    import asyncio
    asyncio.run(workers.drain_theme_code_async(conn, 1, agent_factory=_theme_coder_factory(tr)))

    agg_result = ThemeAggregationResult(themes=[])
    workers.theme_aggregate_one(conn, 1, agent_factory=_agg_factory(agg_result))
    # Second call: UNIQUE constraint → returns None
    res2 = workers.theme_aggregate_one(conn, 1, agent_factory=_agg_factory(agg_result))
    assert res2 is None


def test_theme_aggregate_one_failure(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    tr = _stub_theme_result([("T1", "desc", ["c1"])])
    import asyncio
    asyncio.run(workers.drain_theme_code_async(conn, 1, agent_factory=_theme_coder_factory(tr)))

    def bad_factory():
        raise RuntimeError("aggregator failed")

    res = workers.theme_aggregate_one(conn, 1, agent_factory=bad_factory)
    assert res is not None
    assert res["ok"] is False
    assert "aggregator failed" in res["error"]

    row = store.latest_theme_aggregation(conn, 1)
    assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# reset helpers
# ---------------------------------------------------------------------------


def test_reset_unfinished_theme_coder_runs(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    run_id = store.start_theme_coder_run(conn, "tc1", 1)
    store.record_theme_coder_failure(conn, run_id, "oops")
    n = store.reset_unfinished_theme_coder_runs(conn, "tc1", codebook_version=1)
    assert n == 1
    count = conn.execute("SELECT COUNT(*) AS n FROM theme_coder_runs").fetchone()["n"]
    assert count == 0


def test_reset_unfinished_theme_aggregations(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    tr = _stub_theme_result([("T1", "d", ["c"])])
    import asyncio
    asyncio.run(workers.drain_theme_code_async(conn, 1, agent_factory=_theme_coder_factory(tr)))

    agg_id = store.start_theme_aggregation(conn, 1)
    store.record_theme_aggregation_failure(conn, agg_id, "boom")
    n = store.reset_unfinished_theme_aggregations(conn, 1)
    assert n == 1
    count = conn.execute("SELECT COUNT(*) AS n FROM theme_aggregations").fetchone()["n"]
    assert count == 0


# ---------------------------------------------------------------------------
# stage2_status_counts
# ---------------------------------------------------------------------------


def test_stage2_status_counts(tmp_path: Path) -> None:
    conn, _ = _init(tmp_path)
    store.add_theme_coder(conn, "tc1", "a")
    store.add_theme_coder(conn, "tc2", "b")

    tr = _stub_theme_result([("T1", "d", ["c1"])])
    import asyncio
    asyncio.run(workers.drain_theme_code_async(conn, 1, agent_factory=_theme_coder_factory(tr)))

    agg_result = ThemeAggregationResult(themes=[
        MergedTheme(name="T1", description="d", original_themes=["T1"], codes=["c1"])
    ])
    workers.theme_aggregate_one(conn, 1, agent_factory=_agg_factory(agg_result))

    sc = store.stage2_status_counts(conn, 1)
    assert sc.theme_coders_total == 2
    assert sc.theme_coder_runs_total == 2
    assert sc.theme_coder_runs_by_status == {"done": 2}
    assert sc.theme_aggregations_total == 1
    assert sc.theme_aggregations_by_status == {"done": 1}
    assert sc.themes_in_result == 1
    assert "done=2" in sc.format()


# ---------------------------------------------------------------------------
# CLI — smoke tests
# ---------------------------------------------------------------------------


def test_cli_init(tmp_path: Path) -> None:
    db = str(tmp_path / "x.sqlite")
    rc = cli_stage2.main(["--db", db, "init"])
    assert rc == 0


def test_cli_add_list_rm_theme_coder(tmp_path: Path) -> None:
    db = str(tmp_path / "x.sqlite")
    cli_stage2.main(["--db", db, "init"])
    assert cli_stage2.main(["--db", db, "add-theme-coder", "tc1", "analyst"]) == 0
    assert cli_stage2.main(["--db", db, "list-theme-coders"]) == 0
    assert cli_stage2.main(["--db", db, "rm-theme-coder", "tc1"]) == 0
    assert cli_stage2.main(["--db", db, "list-theme-coders"]) == 0


def test_cli_theme_code_and_aggregate(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "x.sqlite")
    cli_stage2.main(["--db", db, "init"])
    cli_stage2.main(["--db", db, "add-theme-coder", "tc1", "a"])
    cli_stage2.main(["--db", db, "add-theme-coder", "tc2", "b"])

    tr = _stub_theme_result([("T1", "desc", ["c1", "c2"])])
    agg_result = ThemeAggregationResult(themes=[
        MergedTheme(name="T1", description="d", original_themes=["T1"], codes=["c1"])
    ])
    monkeypatch.setattr(workers, "default_theme_coder_factory",
                        lambda cb, tc: _StubThemeCoder(tr))
    monkeypatch.setattr(workers, "default_theme_aggregator_factory",
                        lambda: _StubThemeAggregator(agg_result))

    rc = cli_stage2.main(["--db", db, "theme-code", "--workers", "2"])
    assert rc == 0

    rc = cli_stage2.main(["--db", db, "theme-aggregate"])
    assert rc == 0

    rc = cli_stage2.main(["--db", db, "status"])
    assert rc == 0

    out_file = str(tmp_path / "themes.json")
    rc = cli_stage2.main(["--db", db, "export-themes", "-o", out_file])
    assert rc == 0
    data = json.loads(Path(out_file).read_text())
    assert len(data["themes"]) == 1
    assert data["themes"][0]["name"] == "T1"

    html_file = str(tmp_path / "themes.html")
    rc = cli_stage2.main(["--db", db, "export-themes-html", "-o", html_file])
    assert rc == 0
    html_text = Path(html_file).read_text()
    assert "<!DOCTYPE html>" in html_text
    assert "T1" in html_text
    assert "bulma" in html_text.lower()
    assert "theme-card" in html_text


def test_cli_export_themes_html_requires_aggregation(tmp_path: Path) -> None:
    db = str(tmp_path / "x.sqlite")
    cli_stage2.main(["--db", db, "init"])
    rc = cli_stage2.main(["--db", db, "export-themes-html"])
    assert rc != 0


def test_cli_theme_aggregate_fails_when_coders_not_done(
    tmp_path: Path, capsys
) -> None:
    db = str(tmp_path / "x.sqlite")
    cli_stage2.main(["--db", db, "init"])
    cli_stage2.main(["--db", db, "add-theme-coder", "tc1", "a"])
    # Don't run theme-code — aggregation should refuse
    rc = cli_stage2.main(["--db", db, "theme-aggregate"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "theme-code" in captured.err
