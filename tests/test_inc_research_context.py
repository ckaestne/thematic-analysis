"""Versioned research-context storage + injection into _inc workers."""

from __future__ import annotations

from pathlib import Path

from thematic_analysis.agents.theme_coder import Theme, ThemeResult
from thematic_analysis.agents.theme_aggregator import (
    MergedTheme,
    ThemeAggregationResult,
)
from thematic_analysis.research_context import ResearchContext

from thematic_analysis_inc import workers
from thematic_analysis_inc import db as store


CTX_DESCRIPTION = (
    "Online climate discourse — understand how lay users justify "
    "climate-policy skepticism.\n\n"
    "Research question: What rhetorical strategies do skeptics use to "
    "justify inaction?"
)


def _ctx() -> ResearchContext:
    return ResearchContext(description=CTX_DESCRIPTION)


def test_set_get_clear_research_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "rc.sqlite")

    assert store.get_research_context(conn) is None
    assert store.latest_research_context_version(conn) is None

    ctx = _ctx()
    v1 = store.set_research_context(conn, ctx)
    assert v1 == 1

    loaded = store.get_research_context(conn)
    assert loaded is not None
    version, got = loaded
    assert version == 1
    assert got.description == CTX_DESCRIPTION
    assert got.tailored_prompts == {}
    assert store.latest_research_context_version(conn) == 1

    # Setting again produces a NEW version (history preserved).
    v2 = store.set_research_context(
        conn, ResearchContext(description="different focus")
    )
    assert v2 == 2
    loaded2 = store.get_research_context(conn)
    assert loaded2 is not None
    version2, got2 = loaded2
    assert version2 == 2
    assert got2.description == "different focus"

    # Original version still retrievable by id.
    by_v1 = store.get_research_context(conn, version=1)
    assert by_v1 is not None
    assert by_v1[0] == 1
    assert by_v1[1].description == CTX_DESCRIPTION

    versions = store.list_research_context_versions(conn)
    assert [v["research_context_version"] for v in versions] == [1, 2]

    assert store.clear_research_context(conn) is True
    assert store.get_research_context(conn) is None
    assert store.list_research_context_versions(conn) == []
    assert store.clear_research_context(conn) is False


def test_tailored_prompts_round_trip(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "rc.sqlite")
    ctx = ResearchContext(
        description="study X",
        tailored_prompts={
            "coder": "coder section",
            "reviewer": "reviewer section",
        },
    )
    store.set_research_context(conn, ctx)
    loaded = store.get_research_context(conn)
    assert loaded is not None
    _, got = loaded
    assert got.tailored_prompts["coder"] == "coder section"
    assert got.tailored_prompts["reviewer"] == "reviewer section"


def test_codes_and_queue_capture_rc_version(tmp_path: Path) -> None:
    """Each code/coding_queue row should record the RC version that was
    current when it was produced, and producing more codes after a new RC
    is set should reference the new RC version."""
    conn = store.init_db(tmp_path / "rc.sqlite")
    store.add_coder(conn, name="alice", identity="x")

    # Set initial RC.
    rc_v1 = store.set_research_context(
        conn, ResearchContext(description="first RC")
    )

    # Seed a segment and run the queue sync.
    doc = store.add_document(conn, "doc.md", b"x")
    store.enqueue_segments(conn, [(doc, "seg one", None, None, 0)])
    store.coding.sync_coding_queue(conn)

    rows = conn.execute(
        "SELECT research_context_version FROM coding_queue"
    ).fetchall()
    assert rows and all(
        int(r["research_context_version"]) == rc_v1 for r in rows
    )

    # Set a new RC and add another segment; its new queue row picks up v2.
    rc_v2 = store.set_research_context(
        conn, ResearchContext(description="second RC")
    )
    store.enqueue_segments(conn, [(doc, "seg two", None, None, 1)])
    store.coding.sync_coding_queue(conn)
    rows_by_seg = {
        int(r["segment_id"]): int(r["research_context_version"])
        for r in conn.execute(
            "SELECT segment_id, research_context_version FROM coding_queue"
        ).fetchall()
    }
    # 2 segments, both queue rows present; older one keeps v1; new one is v2.
    assert set(rows_by_seg.values()) == {rc_v1, rc_v2}


class _CapturingThemeCoder:
    last_seen: ResearchContext | None = None

    def __init__(self) -> None:
        self.research_context: ResearchContext | None = None

    def develop_themes(self) -> ThemeResult:
        type(self).last_seen = self.research_context
        return ThemeResult(themes=[Theme(name="t", description="d", codes=["c"])])


class _CapturingThemeAggregator:
    last_seen: ResearchContext | None = None

    def __init__(self) -> None:
        self.research_context: ResearchContext | None = None

    def aggregate(self, theme_results: list[ThemeResult]) -> ThemeAggregationResult:
        type(self).last_seen = self.research_context
        return ThemeAggregationResult(
            themes=[
                MergedTheme(
                    name="t",
                    description="d",
                    original_themes=["t"],
                    codes=["c"],
                    quotes=[],
                )
            ]
        )


def test_theme_code_one_injects_research_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "tc.sqlite")
    store.add_theme_coder(conn, "tc1", "analyst")
    store.set_research_context(conn, _ctx())

    _CapturingThemeCoder.last_seen = None
    res = workers.theme_code_one(
        conn,
        "tc1",
        codebook_version=1,
        agent_factory=lambda codebook, theme_coder: _CapturingThemeCoder(),
    )
    assert res is not None and res["ok"] is True
    seen = _CapturingThemeCoder.last_seen
    assert seen is not None
    assert "rhetorical strategies" in seen.description

    # The theme_coder_run row should reference the latest RC version.
    row = conn.execute(
        "SELECT research_context_version FROM theme_coder_runs "
        "WHERE id = ?",
        (res["run_id"],),
    ).fetchone()
    assert row is not None and int(row["research_context_version"]) == 1


def test_theme_aggregate_one_injects_research_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "ta.sqlite")
    store.add_theme_coder(conn, "tc1", "analyst")
    store.set_research_context(conn, _ctx())

    workers.theme_code_one(
        conn,
        "tc1",
        codebook_version=1,
        agent_factory=lambda codebook, theme_coder: _CapturingThemeCoder(),
    )

    _CapturingThemeAggregator.last_seen = None
    res = workers.theme_aggregate_one(
        conn,
        codebook_version=1,
        agent_factory=lambda: _CapturingThemeAggregator(),
    )
    assert res is not None and res["ok"] is True
    seen = _CapturingThemeAggregator.last_seen
    assert seen is not None
    assert "climate" in seen.description.lower()

    row = conn.execute(
        "SELECT research_context_version FROM theme_aggregations "
        "WHERE id = ?",
        (res["aggregation_id"],),
    ).fetchone()
    assert row is not None and int(row["research_context_version"]) == 1


def test_workers_skip_injection_when_no_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "noctx.sqlite")
    store.add_theme_coder(conn, "tc1", "analyst")

    _CapturingThemeCoder.last_seen = None
    workers.theme_code_one(
        conn,
        "tc1",
        codebook_version=1,
        agent_factory=lambda codebook, theme_coder: _CapturingThemeCoder(),
    )
    assert _CapturingThemeCoder.last_seen is None


def test_theme_aggregator_agent_includes_context_in_prompt() -> None:
    from thematic_analysis.agents.theme_aggregator import ThemeAggregatorAgent

    agent = ThemeAggregatorAgent(research_context=_ctx())
    prompt = agent.get_system_prompt()
    assert "Research Context" in prompt
    assert "rhetorical strategies" in prompt


def test_theme_aggregator_agent_uses_tailored_prompt() -> None:
    from thematic_analysis.agents.theme_aggregator import ThemeAggregatorAgent

    ctx = ResearchContext(
        description="raw description",
        tailored_prompts={
            "theme_aggregator": "## Tailored aggregator section\nDo Y."
        },
    )
    agent = ThemeAggregatorAgent(research_context=ctx)
    prompt = agent.get_system_prompt()
    assert "Tailored aggregator section" in prompt
    assert "raw description" not in prompt
