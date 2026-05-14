"""Research context storage + injection into _inc workers."""

from __future__ import annotations

import json
from pathlib import Path

from thematic_analysis.agents.theme_coder import Theme, ThemeResult
from thematic_analysis.agents.theme_aggregator import (
    MergedTheme,
    ThemeAggregationResult,
)
from thematic_analysis.research_context import ResearchContext

from thematic_analysis_inc import store, workers


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

    ctx = _ctx()
    store.set_research_context(conn, ctx)

    loaded = store.get_research_context(conn)
    assert loaded is not None
    assert loaded.description == CTX_DESCRIPTION
    assert loaded.tailored_prompts == {}

    # Upsert overwrites
    ctx2 = ResearchContext(description="different focus")
    store.set_research_context(conn, ctx2)
    loaded2 = store.get_research_context(conn)
    assert loaded2 is not None
    assert loaded2.description == "different focus"

    assert store.clear_research_context(conn) is True
    assert store.get_research_context(conn) is None
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
    assert loaded.tailored_prompts["coder"] == "coder section"
    assert loaded.tailored_prompts["reviewer"] == "reviewer section"


def test_legacy_shape_folded_into_description(tmp_path: Path) -> None:
    """A DB row written by the old 9-field code should still load."""
    conn = store.init_db(tmp_path / "legacy.sqlite")
    legacy_json = json.dumps(
        {
            "title": "Climate Study",
            "aim": "Understand discourse",
            "research_questions": ["What strategies are used?"],
            "theoretical_framework": "Social constructionism",
            "paradigm": "interpretivist",
            "methodology": "thematic_analysis",
            "domain": "climate change",
            "background": "",
            "keywords": ["climate", "skepticism"],
        }
    )
    conn.execute(
        "INSERT INTO research_context (id, context_json, updated_at) "
        "VALUES (1, ?, '2020-01-01T00:00:00+00:00')",
        (legacy_json,),
    )
    loaded = store.get_research_context(conn)
    assert loaded is not None
    assert "Climate Study" in loaded.description
    assert "Understand discourse" in loaded.description
    assert "What strategies are used?" in loaded.description
    assert "Social constructionism" in loaded.description
    assert "climate, skepticism" in loaded.description
    assert loaded.tailored_prompts == {}


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
