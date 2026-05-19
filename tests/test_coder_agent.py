"""Tests for CoderAgent."""

import json
from unittest.mock import patch

import pytest

from thematic_analysis.agents import CodeAssignment, CoderAgent, CoderConfig
from thematic_analysis.codebook import Codebook, Quote
from thematic_analysis.research_context import ResearchContext


CLIMATE_DESCRIPTION = (
    "Climate Change Perceptions Study. "
    "Aim: understand public perceptions, attitudes, and emotional "
    "responses to climate change.\n\n"
    "Research questions:\n"
    "1. How do people perceive and make sense of climate change?\n"
    "2. What emotional responses does climate change evoke?\n"
)


class TestCoderConfig:
    """Test cases for CoderConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = CoderConfig()

        assert config.max_codes_per_segment == 5
        assert config.similarity_threshold == 0.7
        assert config.include_rationale is True
        assert config.temperature == 0.7

    def test_custom_config(self):
        """Test custom configuration values."""
        config = CoderConfig(
            max_codes_per_segment=3,
            similarity_threshold=0.8,
            identity="researcher perspective",
        )

        assert config.max_codes_per_segment == 3
        assert config.similarity_threshold == 0.8
        assert config.identity == "researcher perspective"


class TestCodeAssignment:
    """Test cases for CodeAssignment."""

    def test_code_assignment_creation(self):
        """Test creating a CodeAssignment."""
        assignment = CodeAssignment(
            segment_id="seg1",
            segment_text="sample text",
            codes=["code1", "code2"],
            quotes=["excerpt one", "excerpt two"],
            rationales=["reason1", "reason2"],
            is_new_code=[True, False],
        )

        assert assignment.segment_id == "seg1"
        assert len(assignment.codes) == 2
        assert len(assignment.quotes) == 2
        assert assignment.quotes == ["excerpt one", "excerpt two"]
        assert len(assignment.rationales) == 2

    def test_code_assignment_defaults(self):
        """Test CodeAssignment default values."""
        assignment = CodeAssignment(
            segment_id="seg1", segment_text="text", codes=["code1"]
        )

        assert assignment.quotes == []
        assert assignment.rationales == []
        assert assignment.is_new_code == []


class TestCoderAgent:
    """Test cases for CoderAgent."""

    @pytest.fixture
    def agent(self) -> CoderAgent:
        """Create a CoderAgent for tests."""
        return CoderAgent()

    @pytest.fixture
    def agent_with_codebook(self) -> CoderAgent:
        """Create a CoderAgent with pre-populated codebook."""
        codebook = Codebook()
        codebook.add_code("emotional support", [Quote("q1", "felt supported")])
        codebook.add_code("peer connection", [Quote("q2", "connected with peers")])
        return CoderAgent(codebook=codebook)

    def test_initialization(self, agent: CoderAgent):
        """Test agent initialization."""
        assert agent.codebook is not None
        assert len(agent.codebook) == 0
        assert isinstance(agent.coder_config, CoderConfig)

    def test_initialization_with_codebook(self, agent_with_codebook: CoderAgent):
        """Test agent initialization with existing codebook."""
        assert len(agent_with_codebook.codebook) == 2

    def test_get_system_prompt_without_identity(self, agent: CoderAgent):
        """Test system prompt generation without identity."""
        prompt = agent.get_system_prompt()

        assert "qualitative researcher" in prompt
        assert "Guidelines for Coding" in prompt
        assert "Your Perspective" not in prompt

    def test_get_system_prompt_with_identity(self):
        """Test system prompt generation with identity."""
        config = CoderConfig(identity="feminist researcher")
        agent = CoderAgent(config=config)
        prompt = agent.get_system_prompt()

        assert "Your Perspective" in prompt
        assert "feminist researcher" in prompt

    def test_format_codebook_section_empty(self, agent: CoderAgent):
        """Test formatting empty codebook."""
        section = agent._format_codebook_section()

        assert "empty" in section.lower()

    def test_format_codebook_section_with_codes(self, agent_with_codebook: CoderAgent):
        """Test formatting codebook with codes."""
        section = agent_with_codebook._format_codebook_section()

        assert "emotional support" in section
        assert "peer connection" in section
        assert "2 total" in section

    def test_format_similar_codes_section_empty_codebook(self, agent: CoderAgent):
        """Test similar codes section with empty codebook."""
        section = agent._format_similar_codes_section("test text")

        assert section == ""

    def test_parse_response_valid_json(self, agent: CoderAgent):
        """Test parsing valid JSON response."""
        response = """```json
{
  "codes": ["code1", "code2"],
  "quotes": ["excerpt one", "excerpt two"],
  "rationales": ["reason1", "reason2"],
  "is_new": [true, false]
}
```"""
        result = agent._parse_response(response, "seg1")

        assert result is not None
        assert result.codes == ["code1", "code2"]
        assert result.quotes == ["excerpt one", "excerpt two"]
        assert result.rationales == ["reason1", "reason2"]
        assert result.is_new_code == [True, False]

    def test_parse_response_raw_json(self, agent: CoderAgent):
        """Test parsing raw JSON without code blocks."""
        response = '{"codes": ["code1"], "quotes": ["excerpt"], "rationales": ["reason1"], "is_new": [true]}'
        result = agent._parse_response(response, "seg1")

        assert result is not None
        assert result.codes == ["code1"]
        assert result.quotes == ["excerpt"]

    def test_parse_response_invalid_json(self, agent: CoderAgent):
        """Test parsing invalid JSON returns None."""
        response = "This is not valid JSON"
        result = agent._parse_response(response, "seg1")

        assert result is None

    def test_parse_response_missing_fields(self, agent: CoderAgent):
        """Test parsing response with missing optional fields."""
        response = '{"codes": ["code1"]}'
        result = agent._parse_response(response, "seg1")

        assert result is not None
        assert result.codes == ["code1"]
        assert len(result.quotes) >= len(result.codes)
        assert len(result.rationales) >= len(result.codes)
        assert len(result.is_new_code) >= len(result.codes)

    def test_parse_response_truncates_to_max_codes(self):
        """Test that parsing respects max_codes_per_segment."""
        config = CoderConfig(max_codes_per_segment=2)
        agent = CoderAgent(config=config)

        response = '{"codes": ["c1", "c2", "c3", "c4"], "quotes": ["q1", "q2", "q3", "q4"], "rationales": [], "is_new": []}'
        result = agent._parse_response(response, "seg1")

        assert result is not None
        assert len(result.codes) == 2
        assert len(result.quotes) == 2

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segment(self, mock_llm, agent: CoderAgent):
        """Test coding a single segment."""
        mock_llm.return_value = json.dumps(
            {
                "codes": ["test code"],
                "quotes": ["this is test"],
                "rationales": ["test reason"],
                "is_new": [True],
            }
        )

        result = agent.code_segment("seg1", "This is test text")

        assert result.segment_id == "seg1"
        assert result.segment_text == "This is test text"
        assert result.codes == ["test code"]
        assert result.quotes == ["this is test"]
        mock_llm.assert_called_once()

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segment_fallback_on_parse_error(self, mock_llm, agent: CoderAgent):
        """Test coding returns empty assignment on parse error."""
        mock_llm.return_value = "Invalid response"

        result = agent.code_segment("seg1", "Test text")

        assert result.segment_id == "seg1"
        assert result.codes == []
        assert result.quotes == []

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segment_fallback_quote_when_missing(self, mock_llm, agent: CoderAgent):
        """Test that missing quotes are filled with segment text as fallback."""
        mock_llm.return_value = json.dumps(
            {
                "codes": ["code1", "code2"],
                "quotes": [],
                "rationales": ["r1", "r2"],
                "is_new": [True, True],
            }
        )

        result = agent.code_segment("seg1", "The full segment text here")

        assert len(result.quotes) == 2
        assert result.quotes[0] == "The full segment text here"
        assert result.quotes[1] == "The full segment text here"

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segment_quotes_present_in_response(self, mock_llm, agent: CoderAgent):
        """Test that extracted quotes are preserved when present."""
        mock_llm.return_value = json.dumps(
            {
                "codes": ["peer support", "resilience"],
                "quotes": ["friends helped me through it", "I bounced back quickly"],
                "rationales": ["Social support", "Coping ability"],
                "is_new": [False, True],
            }
        )

        result = agent.code_segment("seg1", "My friends helped me through it and I bounced back quickly.")

        assert result.quotes == ["friends helped me through it", "I bounced back quickly"]

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segments_updates_codebook(self, mock_llm, agent: CoderAgent):
        """Test that coding multiple segments updates the codebook."""
        mock_llm.return_value = json.dumps(
            {
                "codes": ["new code"],
                "quotes": ["some excerpt"],
                "rationales": ["reason"],
                "is_new": [True],
            }
        )

        segments = [("seg1", "text1"), ("seg2", "text2")]
        results = agent.code_segments(segments)

        assert len(results) == 2
        # Per paper architecture, coders don't update codebook directly
        # Codebook updates flow through Aggregator -> Reviewer
        assert len(agent.codebook) == 0

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segments_does_not_update_codebook(self, mock_llm, agent: CoderAgent):
        """Test that coding does not update codebook (per paper architecture).

        Per Figure 2 of the paper, coders produce code assignments which flow
        to the Aggregator, then the Reviewer maintains the codebook.
        """
        mock_llm.return_value = json.dumps(
            {
                "codes": ["code"],
                "quotes": ["an excerpt"],
                "rationales": ["reason"],
                "is_new": [True],
            }
        )

        segments = [("seg1", "text1")]
        results = agent.code_segments(segments)

        # Coder produces assignments but doesn't touch codebook
        assert len(results) == 1
        assert len(agent.codebook) == 0


class TestCoderAgentResearchContext:
    """Test cases for CoderAgent with research context."""

    def test_initialization_with_research_context(self):
        """Test agent initialization with research context."""
        ctx = ResearchContext(description=CLIMATE_DESCRIPTION)
        agent = CoderAgent(research_context=ctx)

        assert agent.research_context is not None
        assert "Climate" in agent.research_context.description

    def test_set_research_context(self):
        """Test setting research context after initialization."""
        agent = CoderAgent()
        assert agent.research_context is None

        ctx = ResearchContext(description="Test Study. To test.")
        agent.set_research_context(ctx)

        assert agent.research_context is not None
        assert "Test Study" in agent.research_context.description

    def test_system_prompt_includes_research_context(self):
        """Test that system prompt includes research context."""
        ctx = ResearchContext(
            description=(
                "Climate Study. To understand climate perceptions through "
                "the lens of social constructionism. Research question: "
                "How do people perceive climate change?"
            ),
        )
        agent = CoderAgent(research_context=ctx)
        prompt = agent.get_system_prompt()

        assert "## Research Context" in prompt
        assert "Climate Study" in prompt
        assert "How do people perceive climate change?" in prompt
        assert "social constructionism" in prompt

    def test_system_prompt_uses_tailored_prompt_for_coder(self):
        ctx = ResearchContext(
            description="raw description",
            tailored_prompts={"coder": "## Tailored coder section\nDo X."},
        )
        agent = CoderAgent(research_context=ctx)
        prompt = agent.get_system_prompt()
        assert "Tailored coder section" in prompt
        assert "raw description" not in prompt

    def test_system_prompt_without_research_context(self):
        """Test that system prompt works without research context."""
        agent = CoderAgent()
        prompt = agent.get_system_prompt()

        assert "## Research Context" not in prompt
        assert "qualitative researcher" in prompt

    def test_system_prompt_with_empty_research_context(self):
        """Test that empty research context is not included."""
        ctx = ResearchContext()  # Empty context
        agent = CoderAgent(research_context=ctx)
        prompt = agent.get_system_prompt()

        assert "## Research Context" not in prompt

    def test_system_prompt_with_identity_and_research_context(self):
        """Test combining identity with research context."""
        ctx = ResearchContext(
            description="Healthcare Study. To understand patient experiences."
        )
        config = CoderConfig(identity="patient advocate")
        agent = CoderAgent(config=config, research_context=ctx)
        prompt = agent.get_system_prompt()

        # Should have both sections
        assert "Research Context" in prompt
        assert "Healthcare Study" in prompt
        assert "Your Perspective" in prompt
        assert "patient advocate" in prompt

    def test_config_has_6rs_guidance_option(self):
        """Test that config has 6rs guidance option."""
        config = CoderConfig()
        assert hasattr(config, "include_6rs_guidance")
        assert config.include_6rs_guidance is True

        config2 = CoderConfig(include_6rs_guidance=False)
        assert config2.include_6rs_guidance is False

    @patch.object(CoderAgent, "_call_llm")
    def test_coding_with_research_context(self, mock_llm):
        """Test that coding works with research context."""
        mock_llm.return_value = json.dumps(
            {
                "codes": ["climate anxiety", "environmental concern"],
                "quotes": ["worry about the future", "future of our planet"],
                "rationales": ["Expresses worry about climate", "Shows concern"],
                "is_new": [True, True],
            }
        )

        ctx = ResearchContext(description=CLIMATE_DESCRIPTION)
        agent = CoderAgent(research_context=ctx)
        result = agent.code_segment("seg1", "I worry about the future of our planet.")

        assert result.segment_id == "seg1"
        assert len(result.codes) == 2
        assert len(result.quotes) == 2
        mock_llm.assert_called_once()
        # Verify research context was included in the call
        call_args = mock_llm.call_args
        system_prompt = call_args[0][0]
        assert "Climate" in system_prompt
