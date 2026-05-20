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
    store.init_db(tmp_path / "rc.sqlite")

    # init_db seeds an empty research-context revision (v1) so the queue
    # primary key always has a valid revision to point at.
    seed = store.get_research_context()
    assert seed is not None
    assert seed.research_context_version == 1
    assert seed.description == ""

    ctx = _ctx()
    rc2 = store.set_research_context(ctx)
    assert rc2.research_context_version == 2

    got = store.get_research_context()
    assert got is not None
    assert got.research_context_version == 2
    domain = store.research_context_to_domain(got)
    assert domain.description == CTX_DESCRIPTION
    assert domain.tailored_prompts == {}
    assert store.latest_research_context_version() == 2

    # Setting again produces a NEW version (history preserved).
    rc3 = store.set_research_context(ResearchContext(description="different focus"))
    assert rc3.research_context_version == 3
    got3 = store.get_research_context()
    assert got3 is not None and got3.research_context_version == 3
    assert got3.description == "different focus"

    # Original versions still retrievable by id.
    by_v2 = store.get_research_context(version=2)
    assert by_v2 is not None and by_v2.description == CTX_DESCRIPTION

    versions = store.list_research_context_versions()
    assert [r.research_context_version for r in versions] == [1, 2, 3]

    assert store.clear_research_context() is True
    assert store.get_research_context() is None
    assert store.list_research_context_versions() == []
    assert store.clear_research_context() is False


def test_tailored_prompts_round_trip(tmp_path: Path) -> None:
    store.init_db(tmp_path / "rc.sqlite")
    ctx = ResearchContext(
        description="study X",
        tailored_prompts={
            "coder": "coder section",
            "reviewer": "reviewer section",
        },
    )
    store.set_research_context(ctx)
    rc = store.get_research_context()
    assert rc is not None
    got = store.research_context_to_domain(rc)
    assert got.tailored_prompts["coder"] == "coder section"
    assert got.tailored_prompts["reviewer"] == "reviewer section"


def test_codes_and_queue_capture_codebook_version(tmp_path: Path) -> None:
    """Each coding_queue row should record the codebook version that was
    current when it was enqueued. Setting a new research context creates
    a fresh codebook revision, so re-enqueueing pins the new codebook
    (existing rows at the old codebook stay as history). The research
    context active for a queue row is reachable via the codebook."""
    conn = store.init_db(tmp_path / "rc.sqlite")
    store.add_coder("x")

    # Set initial RC → also creates a codebook revision.
    store.set_research_context(ResearchContext(description="first RC"))
    cb_v1 = store.latest_codebook().version

    # Seed a segment and enqueue.
    doc = store.add_document("doc.md")
    store.enqueue_segments(doc, [(None, "seg one", 0, 0, 0)])
    store.coding.enqueue_document(doc.document_id)

    rows = conn.execute(
        "SELECT codebook_used_id FROM coding_queue"
    ).fetchall()
    assert rows and all(int(r["codebook_used_id"]) == cb_v1 for r in rows)

    # New RC bumps the codebook to v2. Adding another segment and
    # re-enqueueing creates fresh queue rows pinned to cb_v2.
    store.set_research_context(ResearchContext(description="second RC"))
    cb_v2 = store.latest_codebook().version
    assert cb_v2 > cb_v1
    store.enqueue_segments(doc, [(None, "seg two", 0, 0, 1)])
    store.coding.enqueue_document(doc.document_id)
    seg_cb_pairs = {
        (int(r["segment_id"]), int(r["codebook_used_id"]))
        for r in conn.execute(
            "SELECT segment_id, codebook_used_id FROM coding_queue"
        ).fetchall()
    }
    # seg-one has a row at cb_v1 (original) AND cb_v2 (re-enqueued under
    # the new codebook). seg-two only has cb_v2 (added after the bump).
    seg_ids = sorted({sid for sid, _ in seg_cb_pairs})
    assert seg_cb_pairs == {
        (seg_ids[0], cb_v1),
        (seg_ids[0], cb_v2),
        (seg_ids[1], cb_v2),
    }


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
    store.set_research_context(_ctx())
    cb_version = store.latest_codebook().version

    _CapturingThemeCoder.last_seen = None
    res = workers.theme_code_one(
        conn,
        "tc1",
        codebook_version=cb_version,
        agent_factory=lambda codebook, theme_coder: _CapturingThemeCoder(),
    )
    assert res is not None and res["ok"] is True
    seen = _CapturingThemeCoder.last_seen
    assert seen is not None
    assert "rhetorical strategies" in seen.description

    # The theme_coder_run row pins the codebook revision; the research
    # context is reached transitively through codebook.research_context.
    row = conn.execute(
        "SELECT codebook_version FROM theme_coder_runs WHERE id = ?",
        (res["run_id"],),
    ).fetchone()
    assert row is not None and int(row["codebook_version"]) == cb_version
    cb = store.get_codebook(cb_version)
    assert cb is not None
    rc = store.get_research_context(cb.research_context_version)
    assert rc is not None and "rhetorical strategies" in rc.description


def test_theme_aggregate_one_injects_research_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "ta.sqlite")
    store.add_theme_coder(conn, "tc1", "analyst")
    store.set_research_context(_ctx())
    cb_version = store.latest_codebook().version

    workers.theme_code_one(
        conn,
        "tc1",
        codebook_version=cb_version,
        agent_factory=lambda codebook, theme_coder: _CapturingThemeCoder(),
    )

    _CapturingThemeAggregator.last_seen = None
    res = workers.theme_aggregate_one(
        conn,
        codebook_version=cb_version,
        agent_factory=lambda: _CapturingThemeAggregator(),
    )
    assert res is not None and res["ok"] is True
    seen = _CapturingThemeAggregator.last_seen
    assert seen is not None
    assert "climate" in seen.description.lower()

    row = conn.execute(
        "SELECT codebook_version FROM theme_aggregations WHERE id = ?",
        (res["aggregation_id"],),
    ).fetchone()
    assert row is not None and int(row["codebook_version"]) == cb_version


def test_workers_skip_injection_when_no_context(tmp_path: Path) -> None:
    conn = store.init_db(tmp_path / "noctx.sqlite")
    store.add_theme_coder(conn, "tc1", "analyst")
    cb_version = store.latest_codebook().version

    _CapturingThemeCoder.last_seen = None
    workers.theme_code_one(
        conn,
        "tc1",
        codebook_version=cb_version,
        agent_factory=lambda codebook, theme_coder: _CapturingThemeCoder(),
    )
    assert _CapturingThemeCoder.last_seen is None


def test_theme_aggregator_agent_includes_context_in_prompt() -> None:
    from thematic_analysis.agents.theme_aggregator import ThemeAggregatorAgent

    agent = ThemeAggregatorAgent(research_context=_ctx())
    prompt = agent.get_system_prompt()
    assert "Research Context" in prompt
    assert "rhetorical strategies" in prompt
    assert prompt.index("You are an expert qualitative researcher") < prompt.index(
        "Research Context"
    )


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
