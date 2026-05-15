"""Tests for research context module."""

from thematic_analysis.research_context import (
    AGENT_ROLES,
    CODE_6RS,
    CONCEPTUALIZATION_GUIDANCE,
    KEYWORD_6RS,
    THEME_DEVELOPMENT_GUIDANCE,
    ResearchContext,
    create_methodology_prompt,
)


class TestResearchContext:
    """Tests for the ResearchContext dataclass."""

    def test_empty_context(self):
        ctx = ResearchContext()
        assert ctx.is_empty()
        assert ctx.description == ""
        assert ctx.tailored_prompts == {}

    def test_non_empty_context(self):
        ctx = ResearchContext(description="Study of climate discourse.")
        assert not ctx.is_empty()

    def test_whitespace_only_is_empty(self):
        assert ResearchContext(description="   \n  ").is_empty()

    def test_to_prompt_section_empty(self):
        assert ResearchContext().to_prompt_section() == ""

    def test_to_prompt_section_fallback_to_description(self):
        ctx = ResearchContext(description="Looking at how users justify X.")
        section = ctx.to_prompt_section()
        assert section.startswith("## Research Context")
        assert "Looking at how users justify X." in section

    def test_to_prompt_section_uses_tailored_when_available(self):
        ctx = ResearchContext(
            description="raw description",
            tailored_prompts={"coder": "## Tailored for coder\nDo X."},
        )
        section = ctx.to_prompt_section(role="coder")
        assert section == "## Tailored for coder\nDo X."
        assert "raw description" not in section

    def test_to_prompt_section_falls_back_when_role_missing(self):
        ctx = ResearchContext(
            description="raw description",
            tailored_prompts={"coder": "tailored"},
        )
        section = ctx.to_prompt_section(role="reviewer")
        assert "raw description" in section

    def test_agent_roles_expected(self):
        assert "coder" in AGENT_ROLES
        assert "theme_coder" in AGENT_ROLES
        assert "reviewer" in AGENT_ROLES
        assert "theme_aggregator" in AGENT_ROLES


class TestMethodologyPrompt:
    """Tests for create_methodology_prompt."""

    def test_empty_prompt(self):
        prompt = create_methodology_prompt(
            research_context=None,
            include_6rs_keywords=False,
            include_6rs_codes=False,
            include_theme_guidance=False,
            include_conceptualization=False,
        )
        assert prompt == ""

    def test_prompt_with_6rs_codes_default(self):
        prompt = create_methodology_prompt()
        assert "## 6 Rs Framework for Code Quality" in prompt
        assert "Reciprocal" in prompt

    def test_prompt_with_6rs_keywords(self):
        prompt = create_methodology_prompt(include_6rs_keywords=True)
        assert "## 6 Rs Framework for Keyword Selection" in prompt
        assert "Realness" in prompt
        assert "Regal" in prompt

    def test_prompt_with_theme_guidance(self):
        prompt = create_methodology_prompt(include_theme_guidance=True)
        assert "## Theme Development Guidance" in prompt

    def test_prompt_with_conceptualization(self):
        prompt = create_methodology_prompt(include_conceptualization=True)
        assert "## Conceptualization Guidance" in prompt

    def test_prompt_with_research_context(self):
        ctx = ResearchContext(description="A study of X.")
        prompt = create_methodology_prompt(
            research_context=ctx, include_6rs_codes=False
        )
        assert "A study of X." in prompt

    def test_prompt_uses_role_for_tailored(self):
        ctx = ResearchContext(
            description="raw",
            tailored_prompts={"coder": "## tailored coder section"},
        )
        prompt = create_methodology_prompt(
            research_context=ctx, include_6rs_codes=False, role="coder"
        )
        assert "tailored coder section" in prompt
        assert "raw" not in prompt

    def test_prompt_ignores_empty_context(self):
        ctx = ResearchContext()
        prompt = create_methodology_prompt(
            research_context=ctx, include_6rs_codes=False
        )
        assert prompt == ""


class TestGuidanceConstants:
    def test_keyword_6rs_content(self):
        for r in ["Realness", "Richness", "Repetition", "Rationale", "Repartee", "Regal"]:
            assert r in KEYWORD_6RS

    def test_code_6rs_content(self):
        for r in ["Reciprocal", "Recognizable", "Responsive", "Resourceful"]:
            assert r in CODE_6RS

    def test_theme_development_content(self):
        for r in ["Organizing codes", "Considering theory", "Looking for patterns"]:
            assert r in THEME_DEVELOPMENT_GUIDANCE

    def test_conceptualization_content(self):
        for r in ["Interpret coherently", "Build theory", "Connect to literature"]:
            assert r in CONCEPTUALIZATION_GUIDANCE
