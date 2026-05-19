"""Tests for CodeAggregatorAgent."""

import json
from unittest.mock import patch

import pytest

from thematic_analysis.agents import (
    AggregationResult,
    AggregatorConfig,
    CodeAggregatorAgent,
    CodeAssignment,
    MergedCode,
)
from thematic_analysis.codebook import Codebook, Quote


class TestAggregatorConfig:
    """Test cases for AggregatorConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = AggregatorConfig()

        assert config.similarity_threshold == 0.8
        assert config.max_quotes_per_code == 10

    def test_custom_config(self):
        """Test custom configuration values."""
        config = AggregatorConfig(
            similarity_threshold=0.9,
            max_quotes_per_code=5,
        )

        assert config.similarity_threshold == 0.9
        assert config.max_quotes_per_code == 5


class TestMergedCode:
    """Test cases for MergedCode."""

    def test_merged_code_creation(self):
        """Test creating a MergedCode."""
        quotes = [Quote("q1", "text1"), Quote("q2", "text2")]
        merged = MergedCode(
            code="merged code",
            original_codes=["code1", "code2"],
            quotes=quotes,
            merge_rationale="Similar concepts",
        )

        assert merged.code == "merged code"
        assert len(merged.original_codes) == 2
        assert len(merged.quotes) == 2
        assert merged.merge_rationale == "Similar concepts"


class TestAggregationResult:
    """Test cases for AggregationResult."""

    def test_aggregation_result_creation(self):
        """Test creating an AggregationResult."""
        merged = MergedCode(
            code="merged",
            original_codes=["a", "b"],
            quotes=[Quote("q1", "text")],
        )
        retained = MergedCode(
            code="standalone",
            original_codes=["standalone"],
            quotes=[Quote("q2", "text2")],
        )
        result = AggregationResult(
            merged_codes=[merged],
            retained_codes=[retained],
        )

        assert len(result.merged_codes) == 1
        assert len(result.retained_codes) == 1

    def test_to_json(self):
        """Test JSON serialization."""
        merged = MergedCode(
            code="merged",
            original_codes=["a", "b"],
            quotes=[Quote("q1", "text1")],
            merge_rationale="Same concept",
        )
        result = AggregationResult(
            merged_codes=[merged],
            retained_codes=[],
        )

        json_str = result.to_json()
        data = json.loads(json_str)

        assert "merged_codes" in data
        assert len(data["merged_codes"]) == 1
        assert data["merged_codes"][0]["code"] == "merged"
        assert data["merged_codes"][0]["merge_rationale"] == "Same concept"

    def test_all_codes(self):
        """Test all_codes method."""
        merged = MergedCode("m1", ["a"], [])
        retained = MergedCode("r1", ["b"], [])
        result = AggregationResult(
            merged_codes=[merged],
            retained_codes=[retained],
        )

        all_codes = result.all_codes()
        assert len(all_codes) == 2


class TestCodeAggregatorAgent:
    """Test cases for CodeAggregatorAgent."""

    @pytest.fixture
    def agent(self) -> CodeAggregatorAgent:
        """Create a CodeAggregatorAgent for tests."""
        return CodeAggregatorAgent()

    @pytest.fixture
    def sample_assignments(self) -> list[CodeAssignment]:
        """Create sample code assignments."""
        return [
            CodeAssignment(
                segment_id="seg1",
                segment_text="I felt really supported by my friends",
                codes=["peer support", "emotional comfort"],
            ),
            CodeAssignment(
                segment_id="seg2",
                segment_text="My classmates helped me through it",
                codes=["peer support", "academic help"],
            ),
            CodeAssignment(
                segment_id="seg3",
                segment_text="Time pressure was overwhelming",
                codes=["time pressure"],
            ),
        ]

    def test_initialization(self, agent: CodeAggregatorAgent):
        """Test agent initialization."""
        assert agent.codebook is not None
        assert isinstance(agent.aggregator_config, AggregatorConfig)

    def test_get_system_prompt(self, agent: CodeAggregatorAgent):
        """Test system prompt generation."""
        prompt = agent.get_system_prompt()

        assert "qualitative researcher" in prompt
        assert "merge_groups" in prompt
        assert "retain_codes" in prompt

    def test_collect_codes_with_quotes(
        self, agent: CodeAggregatorAgent, sample_assignments: list[CodeAssignment]
    ):
        """Test collecting codes with their quotes."""
        code_quotes = agent._collect_codes_with_quotes(sample_assignments)

        assert "peer support" in code_quotes
        assert len(code_quotes["peer support"]) == 2  # From seg1 and seg2
        assert "emotional comfort" in code_quotes
        assert "time pressure" in code_quotes

    def test_collect_codes_uses_coder_extracted_quotes(self, agent: CodeAggregatorAgent):
        """Test that aggregator uses coder-extracted quotes when present."""
        assignments = [
            CodeAssignment(
                segment_id="seg1",
                segment_text="Full segment text that is long",
                codes=["peer support", "resilience"],
                quotes=["friends helped me", "bounced back quickly"],
            ),
        ]

        code_quotes = agent._collect_codes_with_quotes(assignments)

        assert code_quotes["peer support"][0].text == "friends helped me"
        assert code_quotes["resilience"][0].text == "bounced back quickly"

    def test_collect_codes_falls_back_to_segment_text(self, agent: CodeAggregatorAgent):
        """Test that aggregator falls back to segment text when no quotes extracted."""
        assignments = [
            CodeAssignment(
                segment_id="seg1",
                segment_text="Full segment text",
                codes=["peer support"],
                quotes=[],  # No quotes extracted
            ),
        ]

        code_quotes = agent._collect_codes_with_quotes(assignments)

        assert code_quotes["peer support"][0].text == "Full segment text"

    def test_collect_codes_avoids_duplicates(self, agent: CodeAggregatorAgent):
        """Test that duplicate quotes are avoided."""
        assignments = [
            CodeAssignment(
                segment_id="seg1",
                segment_text="test text",
                codes=["code1", "code1"],  # Same code twice
            ),
        ]

        code_quotes = agent._collect_codes_with_quotes(assignments)

        assert len(code_quotes["code1"]) == 1

    def test_find_similar_groups_single_code(self, agent: CodeAggregatorAgent):
        """Test grouping with a single code."""
        groups = agent._find_similar_groups(["single code"])

        assert len(groups) == 1
        assert groups[0] == ["single code"]

    def test_find_similar_groups_empty(self, agent: CodeAggregatorAgent):
        """Test grouping with empty input."""
        groups = agent._find_similar_groups([])

        assert groups == []

    def test_format_codes_section(self, agent: CodeAggregatorAgent):
        """Test formatting codes section."""
        code_quotes = {
            "test code": [Quote("q1", "sample quote text")],
        }

        section = agent._format_codes_section(code_quotes)

        assert "test code" in section
        assert "1 quotes" in section

    def test_format_similar_groups_section(self, agent: CodeAggregatorAgent):
        """Test formatting similar groups."""
        groups = [["code1", "code2"], ["standalone"]]

        section = agent._format_similar_groups_section(groups)

        assert "Group 1:" in section
        assert "code1, code2" in section
        assert "Standalone:" in section

    def test_format_similar_groups_empty(self, agent: CodeAggregatorAgent):
        """Test formatting empty groups."""
        section = agent._format_similar_groups_section([])

        assert "No similar groups" in section

    def test_parse_response_valid_json(self, agent: CodeAggregatorAgent):
        """Test parsing valid JSON response."""
        code_quotes = {
            "code1": [Quote("q1", "text1")],
            "code2": [Quote("q2", "text2")],
            "standalone": [Quote("q3", "text3")],
        }

        response = """```json
{
  "merge_groups": [
    {
      "merged_code": "combined code",
      "original_codes": ["code1", "code2"],
      "rationale": "Similar concepts"
    }
  ],
  "retain_codes": ["standalone"]
}
```"""

        result = agent._parse_response(response, code_quotes)

        assert result is not None
        assert len(result.merged_codes) == 1
        assert result.merged_codes[0].code == "combined code"
        assert len(result.merged_codes[0].original_codes) == 2
        assert len(result.retained_codes) == 1
        assert result.retained_codes[0].code == "standalone"

    def test_parse_response_raw_json(self, agent: CodeAggregatorAgent):
        """Test parsing raw JSON without code blocks."""
        code_quotes = {"code1": [Quote("q1", "text")]}

        response = '{"merge_groups": [], "retain_codes": ["code1"]}'
        result = agent._parse_response(response, code_quotes)

        assert result is not None
        assert len(result.retained_codes) == 1

    def test_parse_response_invalid_json(self, agent: CodeAggregatorAgent):
        """Test parsing invalid JSON returns None."""
        result = agent._parse_response("Not JSON", {})

        assert result is None

    def test_parse_response_respects_max_quotes(self):
        """Test that parsing respects max_quotes_per_code."""
        config = AggregatorConfig(max_quotes_per_code=2)
        agent = CodeAggregatorAgent(config=config)

        code_quotes = {
            "code1": [Quote(f"q{i}", f"text{i}") for i in range(5)],
        }

        response = '{"merge_groups": [], "retain_codes": ["code1"]}'
        result = agent._parse_response(response, code_quotes)

        assert result is not None
        assert len(result.retained_codes[0].quotes) == 2

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate(
        self,
        mock_llm,
        agent: CodeAggregatorAgent,
        sample_assignments: list[CodeAssignment],
    ):
        """Test aggregating codes with negotiation disabled."""
        mock_llm.return_value = json.dumps(
            {
                "merge_groups": [
                    {
                        "merged_code": "peer support system",
                        "original_codes": ["peer support", "emotional comfort"],
                        "rationale": "Both relate to support from peers",
                    }
                ],
                "retain_codes": ["academic help", "time pressure"],
            }
        )

        # Disable negotiation to test raw aggregation behavior
        result = agent.aggregate(sample_assignments, apply_negotiation=False)

        assert len(result.merged_codes) == 1
        assert result.merged_codes[0].code == "peer support system"
        assert len(result.retained_codes) == 2
        mock_llm.assert_called_once()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_with_negotiation(
        self,
        mock_llm,
        agent: CodeAggregatorAgent,
        sample_assignments: list[CodeAssignment],
    ):
        """Test aggregating codes with negotiation strategy (default CONSENSUS).

        With CONSENSUS strategy, only codes agreed by majority are included.
        - 'peer support' appears in 2/3 assignments → included
        - 'emotional comfort' appears in 1/3 assignments → excluded
        - 'academic help' appears in 1/3 assignments → excluded
        - 'time pressure' appears in 1/3 assignments → excluded
        """
        mock_llm.return_value = json.dumps(
            {
                "merge_groups": [],
                "retain_codes": ["peer support"],  # Only agreed code
            }
        )

        result = agent.aggregate(sample_assignments, apply_negotiation=True)

        # With consensus, only 'peer support' passes (2/3 agreement)
        assert len(result.merged_codes) == 0
        assert len(result.retained_codes) == 1
        assert result.retained_codes[0].code == "peer support"
        mock_llm.assert_called_once()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_empty_input(self, mock_llm, agent: CodeAggregatorAgent):
        """Test aggregating empty assignments."""
        result = agent.aggregate([])

        assert len(result.merged_codes) == 0
        assert len(result.retained_codes) == 0
        mock_llm.assert_not_called()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_fallback_on_parse_error(
        self,
        mock_llm,
        agent: CodeAggregatorAgent,
        sample_assignments: list[CodeAssignment],
    ):
        """Test fallback when parsing fails."""
        mock_llm.return_value = "Invalid response"

        result = agent.aggregate(sample_assignments)

        # Should return all codes as retained
        assert len(result.merged_codes) == 0
        assert len(result.retained_codes) > 0

    def test_update_codebook(self, agent: CodeAggregatorAgent):
        """Test updating codebook with aggregation result."""
        merged = MergedCode(
            code="merged code",
            original_codes=["a", "b"],
            quotes=[Quote("q1", "text1")],
        )
        retained = MergedCode(
            code="retained code",
            original_codes=["c"],
            quotes=[Quote("q2", "text2")],
        )
        result = AggregationResult(
            merged_codes=[merged],
            retained_codes=[retained],
        )

        new_codebook = agent.update_codebook(result)

        assert len(new_codebook) == 2
        assert "merged code" in new_codebook.codes
        assert "retained code" in new_codebook.codes


class TestCodeAggregatorIntegration:
    """Integration tests for CodeAggregatorAgent with Codebook."""

    def test_similarity_grouping_with_real_embeddings(self):
        """Test similarity grouping uses real embeddings."""
        codebook = Codebook()
        # Pre-populate codebook to initialize embedding service
        codebook.add_code("test", [Quote("q1", "test")])

        agent = CodeAggregatorAgent(codebook=codebook)

        # These codes should have some similarity
        codes = ["emotional support", "emotional help", "technical assistance"]
        groups = agent._find_similar_groups(codes)

        # Should group based on semantic similarity
        assert len(groups) >= 1

    def test_full_aggregation_workflow(self):
        """Test the full aggregation workflow with real components."""
        codebook = Codebook()
        agent = CodeAggregatorAgent(codebook=codebook)

        # Pre-compute similarity to verify grouping works
        sim = agent.codebook.embedding_service.compute_similarity(
            "exam anxiety", "test stress"
        )

        # These should be somewhat similar
        assert sim > 0.5  # Reasonable threshold for related concepts
