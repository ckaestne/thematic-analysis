"""Research context storage + injection into _inc workers."""

from __future__ import annotations

from pathlib import Path

from thematic_analysis.agents.theme_coder import Theme, ThemeResult
from thematic_analysis.agents.theme_aggregator import (
    MergedTheme,
    ThemeAggregationResult,
)
from thematic_analysis.research_context import ResearchContext

from thematic_analysis_inc import store, workers


def _ctx() -> ResearchContext:
    return ResearchContext(
        title="Online Climate Discourse",
        aim="Understand how lay users justify climate-policy skepticism",
        research_questions=[
            "What rhetorical strategies do skeptics use to justify inaction?"
        ],
        domain="climate change",
    )


def test_set_get_clear_research_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "rc.sqlite")

    assert store.get_research_context(conn) is None

    ctx = _ctx()
    store.set_research_context(conn, ctx)

    loaded = store.get_research_context(conn)
    assert loaded is not None
    assert loaded.title == ctx.title
    assert loaded.research_questions == ctx.research_questions
    assert loaded.domain == "climate change"

    # Upsert overwrites
    ctx2 = ResearchContext(aim="different aim")
    store.set_research_context(conn, ctx2)
    loaded2 = store.get_research_context(conn)
    assert loaded2 is not None
    assert loaded2.aim == "different aim"
    assert loaded2.title == ""

    assert store.clear_research_context(conn) is True
    assert store.get_research_context(conn) is None
    assert store.clear_research_context(conn) is False


class _CapturingThemeCoder:
    """Records research_context attribute observed at develop_themes time."""

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
    assert seen.title == "Online Climate Discourse"
    assert seen.research_questions[0].startswith("What rhetorical")


def test_theme_aggregate_one_injects_research_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "ta.sqlite")
    store.add_theme_coder(conn, "tc1", "analyst")
    store.set_research_context(conn, _ctx())

    # First produce a done theme_coder_run so aggregation has inputs.
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
    assert seen.domain == "climate change"


def test_workers_skip_injection_when_no_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "noctx.sqlite")
    store.add_theme_coder(conn, "tc1", "analyst")
    # Do NOT set a research context.

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
    assert "Online Climate Discourse" in prompt
    assert "rhetorical strategies" in prompt
