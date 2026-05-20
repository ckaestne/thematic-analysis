"""Tests for CoderAgent."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from thematic_analysis.agents import CoderAgent, CoderConfig
from thematic_analysis.codebook import Codebook, Quote as DomainQuote
from thematic_analysis.research_context import ResearchContext
from thematic_analysis_inc.db.models import SENTINEL_CODE_LABEL, is_sentinel_code


def _seg(text: str, quotes=None):
    return SimpleNamespace(segment_id=1, content=text, quotes=quotes or [])


CLIMATE_DESCRIPTION = (
    "Climate Change Perceptions Study. "
    "Aim: understand public perceptions, attitudes, and emotional "
    "responses to climate change.\n\n"
    "Research questions:\n"
    "1. How do people perceive and make sense of climate change?\n"
    "2. What emotional responses does climate change evoke?\n"
)


# Sample segment text used by parser tests; quotes must be substrings.
SEG_TEXT = (
    "I felt supported by my friends and family. "
    "Being connected with peers really helped during that difficult time."
)


def _resp(items: list[dict]) -> str:
    return json.dumps({"codes": items})


class TestCoderConfig:
    def test_default_config(self):
        config = CoderConfig()
        assert config.max_codes_per_segment == 5
        assert config.similarity_threshold == 0.7
        assert config.temperature == 0.7

    def test_custom_config(self):
        config = CoderConfig(
            max_codes_per_segment=3,
            similarity_threshold=0.8,
            identity="researcher perspective",
        )
        assert config.max_codes_per_segment == 3
        assert config.identity == "researcher perspective"


class TestCoderAgent:
    @pytest.fixture
    def agent(self) -> CoderAgent:
        return CoderAgent()

    @pytest.fixture
    def agent_with_codebook(self) -> CoderAgent:
        codebook = Codebook(use_mock_embeddings=True)
        codebook.add_code("emotional support", [DomainQuote("q1", "felt supported")])
        codebook.add_code("peer connection", [DomainQuote("q2", "connected with peers")])
        return CoderAgent(codebook=codebook)

    def test_initialization(self, agent: CoderAgent):
        assert agent.codebook is not None
        assert len(agent.codebook) == 0
        assert isinstance(agent.coder_config, CoderConfig)

    def test_initialization_with_codebook(self, agent_with_codebook: CoderAgent):
        assert len(agent_with_codebook.codebook) == 2

    def test_get_system_prompt_without_identity(self, agent: CoderAgent):
        prompt = agent.get_system_prompt()
        assert "coder in thematic analysis" in prompt
        assert "1–3 codes" in prompt
        assert "Your Perspective" not in prompt

    def test_get_system_prompt_with_identity(self):
        config = CoderConfig(identity="feminist researcher")
        agent = CoderAgent(config=config)
        prompt = agent.get_system_prompt()
        assert "Your Perspective" in prompt
        assert "feminist researcher" in prompt

    def test_format_codebook_section_empty(self, agent: CoderAgent):
        section = agent._format_codebook_section()
        assert "empty" in section.lower()

    def test_format_codebook_section_with_codes(self, agent_with_codebook: CoderAgent):
        section = agent_with_codebook._format_codebook_section()
        assert "emotional support" in section
        assert "peer connection" in section
        assert "2 total" in section

    def test_format_similar_codes_section_empty_codebook(self, agent: CoderAgent):
        assert agent._format_similar_codes_section("test text") == ""

    def test_parse_response_valid_json(self, agent: CoderAgent):
        response = "```json\n" + _resp([
            {
                "code": "emotional support",
                "description": "Receiving comfort from others.",
                "quotes": ["felt supported by my friends and family"],
            },
            {
                "code": "peer connection",
                "description": "Building ties with peers.",
                "quotes": ["connected with peers really helped"],
            },
        ]) + "\n```"
        result = agent._parse_response(response, _seg(SEG_TEXT))
        assert result is not None
        assert [c.code for c in result] == ["emotional support", "peer connection"]
        assert result[0].description.startswith("Receiving")
        assert [q.text for q in result[0].supporting_quotes] == [
            "felt supported by my friends and family"
        ]

    def test_parse_response_raw_json(self, agent: CoderAgent):
        response = _resp([
            {
                "code": "emotional support",
                "description": "Comfort from others.",
                "quotes": ["felt supported"],
            }
        ])
        result = agent._parse_response(response, _seg(SEG_TEXT))
        assert result is not None and len(result) == 1
        assert result[0].code == "emotional support"

    def test_parse_response_invalid_json(self, agent: CoderAgent):
        result = agent._parse_response("Not JSON", _seg(SEG_TEXT))
        assert len(result) == 1 and is_sentinel_code(result[0])

    def test_parse_response_drops_quotes_not_in_segment(self, agent: CoderAgent):
        response = _resp([
            {
                "code": "off-topic",
                "description": "...",
                "quotes": ["text not present in segment"],
            }
        ])
        # Quote isn't a substring; code is dropped, sentinel emitted.
        result = agent._parse_response(response, _seg(SEG_TEXT))
        assert len(result) == 1 and is_sentinel_code(result[0])

    def test_parse_response_truncates_to_max_codes(self):
        config = CoderConfig(max_codes_per_segment=2)
        agent = CoderAgent(config=config)
        response = _resp([
            {"code": f"c{i}", "description": "d", "quotes": ["felt supported"]}
            for i in range(4)
        ])
        result = agent._parse_response(response, _seg(SEG_TEXT))
        assert result is not None
        assert len(result) == 2

    def test_parse_response_empty_codes(self, agent: CoderAgent):
        result = agent._parse_response(_resp([]), _seg(SEG_TEXT))
        assert len(result) == 1 and is_sentinel_code(result[0])

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segment(self, mock_llm, agent: CoderAgent):
        mock_llm.return_value = _resp([
            {
                "code": "emotional support",
                "description": "Comfort from others.",
                "quotes": ["felt supported by my friends"],
            }
        ])
        result = agent.code_segment(_seg(SEG_TEXT))
        assert len(result) == 1
        assert result[0].code == "emotional support"
        assert result[0].supporting_quotes[0].text == "felt supported by my friends"
        mock_llm.assert_called_once()

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segment_fallback_on_parse_error(self, mock_llm, agent: CoderAgent):
        mock_llm.return_value = "Invalid response"
        result = agent.code_segment(_seg(SEG_TEXT))
        assert len(result) == 1 and is_sentinel_code(result[0])

    @patch.object(CoderAgent, "_call_llm")
    def test_code_segments_does_not_update_codebook(self, mock_llm, agent: CoderAgent):
        mock_llm.return_value = _resp([
            {
                "code": "emotional support",
                "description": "Comfort.",
                "quotes": ["felt supported"],
            }
        ])
        results = agent.code_segments([_seg(SEG_TEXT)])
        assert len(results) == 1
        # Coder doesn't touch codebook directly.
        assert len(agent.codebook) == 0


class TestCoderAgentResearchContext:
    def test_initialization_with_research_context(self):
        ctx = ResearchContext(description=CLIMATE_DESCRIPTION)
        agent = CoderAgent(research_context=ctx)
        assert agent.research_context is not None
        assert "Climate" in agent.research_context.description

    def test_set_research_context(self):
        agent = CoderAgent()
        assert agent.research_context is None
        ctx = ResearchContext(description="Test Study. To test.")
        agent.set_research_context(ctx)
        assert agent.research_context is not None
        assert "Test Study" in agent.research_context.description

    def test_system_prompt_includes_research_context(self):
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
        agent = CoderAgent()
        prompt = agent.get_system_prompt()
        assert "## Research Context" not in prompt
        assert "coder in thematic analysis" in prompt

    def test_system_prompt_with_empty_research_context(self):
        ctx = ResearchContext()
        agent = CoderAgent(research_context=ctx)
        prompt = agent.get_system_prompt()
        assert "## Research Context" not in prompt

    def test_system_prompt_with_identity_and_research_context(self):
        ctx = ResearchContext(
            description="Healthcare Study. To understand patient experiences."
        )
        config = CoderConfig(identity="patient advocate")
        agent = CoderAgent(config=config, research_context=ctx)
        prompt = agent.get_system_prompt()
        assert "Research Context" in prompt
        assert "Healthcare Study" in prompt
        assert "Your Perspective" in prompt
        assert "patient advocate" in prompt
        assert prompt.index("You are a coder in thematic analysis") < prompt.index(
            "Research Context"
        )
        assert prompt.index("Research Context") < prompt.index("Your Perspective")

    def test_config_has_6rs_guidance_option(self):
        config = CoderConfig()
        assert hasattr(config, "include_6rs_guidance")
        assert config.include_6rs_guidance is True
        assert CoderConfig(include_6rs_guidance=False).include_6rs_guidance is False

    @patch.object(CoderAgent, "_call_llm")
    def test_coding_with_research_context(self, mock_llm):
        seg = "I worry about the future of our planet every single day."
        mock_llm.return_value = _resp([
            {
                "code": "climate anxiety",
                "description": "Worry about climate-related futures.",
                "quotes": ["worry about the future of our planet"],
            }
        ])
        ctx = ResearchContext(description=CLIMATE_DESCRIPTION)
        agent = CoderAgent(research_context=ctx)
        result = agent.code_segment(_seg(seg))
        assert len(result) == 1
        assert result[0].code == "climate anxiety"
        mock_llm.assert_called_once()
        call_args = mock_llm.call_args
        system_prompt = call_args[0][0]
        assert "Climate" in system_prompt
