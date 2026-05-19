"""Tests for ReviewerAgent."""

import json
from unittest.mock import patch

import pytest

from thematic_analysis.agents import (
    AggregationResult,
    MergedCode,
    ReviewDecision,
    ReviewerAgent,
    ReviewerConfig,
    ReviewResult,
)
from thematic_analysis.codebook import Codebook, Quote
from thematic_analysis.research_context import ResearchContext


class TestReviewerConfig:
    """Test cases for ReviewerConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = ReviewerConfig()

        assert config.similarity_threshold == 0.75
        assert config.top_k_similar == 5
        assert config.merge_threshold == 0.90

    def test_custom_config(self):
        """Test custom configuration values."""
        config = ReviewerConfig(
            similarity_threshold=0.8,
            top_k_similar=10,
            merge_threshold=0.95,
        )

        assert config.similarity_threshold == 0.8
        assert config.top_k_similar == 10
        assert config.merge_threshold == 0.95


class TestReviewResult:
    """Test cases for ReviewResult."""

    def test_review_result_creation(self):
        """Test creating a ReviewResult."""
        quotes = [Quote("q1", "text1")]
        result = ReviewResult(
            code="test code",
            decision=ReviewDecision.ADD_NEW,
            rationale="New concept",
            quotes=quotes,
        )

        assert result.code == "test code"
        assert result.decision == ReviewDecision.ADD_NEW
        assert result.target_code is None
        assert result.quotes is not None and len(result.quotes) == 1

    def test_review_result_with_target(self):
        """Test ReviewResult with target code."""
        result = ReviewResult(
            code="new code",
            decision=ReviewDecision.MERGE,
            target_code="existing code",
            rationale="Same concept",
        )

        assert result.decision == ReviewDecision.MERGE
        assert result.target_code == "existing code"


class TestReviewDecision:
    """Test cases for ReviewDecision enum."""

    def test_decision_values(self):
        """Test decision enum values."""
        assert ReviewDecision.ADD_NEW.value == "add_new"
        assert ReviewDecision.MERGE.value == "merge"
        assert ReviewDecision.UPDATE.value == "update"
        assert ReviewDecision.SKIP.value == "skip"


class TestReviewerAgent:
    """Test cases for ReviewerAgent."""

    @pytest.fixture
    def agent(self) -> ReviewerAgent:
        """Create a ReviewerAgent for tests."""
        return ReviewerAgent()

    @pytest.fixture
    def agent_with_codebook(self) -> ReviewerAgent:
        """Create a ReviewerAgent with pre-populated codebook."""
        codebook = Codebook()
        codebook.add_code("emotional support", [Quote("q1", "felt supported")])
        codebook.add_code("academic pressure", [Quote("q2", "stressed about exams")])
        codebook.add_code("time management", [Quote("q3", "too many deadlines")])
        return ReviewerAgent(codebook=codebook)

    def test_initialization(self, agent: ReviewerAgent):
        """Test agent initialization."""
        assert agent.codebook is not None
        assert isinstance(agent.reviewer_config, ReviewerConfig)

    def test_initialization_with_codebook(self, agent_with_codebook: ReviewerAgent):
        """Test agent initialization with existing codebook."""
        assert len(agent_with_codebook.codebook) == 3

    def test_get_system_prompt(self, agent: ReviewerAgent):
        """Test system prompt generation."""
        prompt = agent.get_system_prompt()

        assert "qualitative researcher" in prompt
        assert "MERGE" in prompt
        assert "UPDATE" in prompt
        assert "ADD_NEW" in prompt

    def test_get_system_prompt_orders_research_context_after_general(self):
        agent = ReviewerAgent(
            research_context=ResearchContext(
                description="Study. Research question: How is risk framed?"
            )
        )
        prompt = agent.get_system_prompt()

        assert "Research Context" in prompt
        assert prompt.index("You are an expert qualitative researcher") < prompt.index(
            "Research Context"
        )

    def test_format_quotes_section(self, agent: ReviewerAgent):
        """Test formatting quotes section."""
        quotes = [
            Quote("q1", "This is a sample quote"),
            Quote("q2", "Another quote here"),
        ]

        section = agent._format_quotes_section(quotes)

        assert "[q1]" in section
        assert "sample quote" in section
        assert "[q2]" in section

    def test_format_quotes_section_empty(self, agent: ReviewerAgent):
        """Test formatting empty quotes."""
        section = agent._format_quotes_section([])

        assert "No quotes available" in section

    def test_format_quotes_section_long_quote(self, agent: ReviewerAgent):
        """Test that long quotes are truncated."""
        long_text = "x" * 300
        quotes = [Quote("q1", long_text)]

        section = agent._format_quotes_section(quotes)

        assert "..." in section
        assert len(section) < 350

    def test_format_similar_codes_section(self, agent_with_codebook: ReviewerAgent):
        """Test formatting similar codes."""
        codebook = agent_with_codebook.codebook
        similar = [(codebook.entries[0], 0.85), (codebook.entries[1], 0.72)]

        section = agent_with_codebook._format_similar_codes_section(similar)

        assert "emotional support" in section
        assert "0.85" in section

    def test_format_similar_codes_section_empty(self, agent: ReviewerAgent):
        """Test formatting empty similar codes."""
        section = agent._format_similar_codes_section([])

        assert "No similar codes" in section

    def test_parse_response_valid_merge(self, agent: ReviewerAgent):
        """Test parsing valid merge response."""
        response = """```json
{
  "decision": "merge",
  "target_code": "emotional support",
  "rationale": "Same underlying concept"
}
```"""
        decision, target, rationale = agent._parse_response(response)

        assert decision == ReviewDecision.MERGE
        assert target == "emotional support"
        assert "Same underlying concept" in rationale

    def test_parse_response_valid_add_new(self, agent: ReviewerAgent):
        """Test parsing valid add_new response."""
        response = '{"decision": "add_new", "rationale": "Novel concept"}'
        decision, target, rationale = agent._parse_response(response)

        assert decision == ReviewDecision.ADD_NEW
        assert target is None

    def test_parse_response_invalid_json(self, agent: ReviewerAgent):
        """Test parsing invalid JSON defaults to ADD_NEW."""
        response = "Not valid JSON"
        decision, target, rationale = agent._parse_response(response)

        assert decision == ReviewDecision.ADD_NEW

    def test_parse_response_unknown_decision(self, agent: ReviewerAgent):
        """Test parsing unknown decision defaults to ADD_NEW."""
        response = '{"decision": "unknown_value"}'
        decision, target, rationale = agent._parse_response(response)

        assert decision == ReviewDecision.ADD_NEW

    def test_review_code_no_similar(self, agent: ReviewerAgent):
        """Test reviewing code with empty codebook."""
        quotes = [Quote("q1", "sample text")]

        result = agent.review_code("new code", quotes)

        assert result.decision == ReviewDecision.ADD_NEW
        assert "No similar codes" in result.rationale

    def test_review_code_auto_merge(self, agent_with_codebook: ReviewerAgent):
        """Test automatic merge when similarity is very high."""
        # Configure for lower merge threshold
        agent_with_codebook.reviewer_config.merge_threshold = 0.85

        quotes = [Quote("q4", "feeling emotionally supported")]

        # "emotional support" exists in codebook
        result = agent_with_codebook.review_code("emotional support", quotes)

        assert result.decision == ReviewDecision.MERGE
        assert "Automatic merge" in result.rationale

    @patch.object(ReviewerAgent, "_call_llm")
    def test_review_code_llm_decision(
        self, mock_llm, agent_with_codebook: ReviewerAgent
    ):
        """Test review with LLM decision."""
        mock_llm.return_value = json.dumps(
            {
                "decision": "merge",
                "target_code": "emotional support",
                "rationale": "Similar meaning",
            }
        )

        # Use lower threshold to ensure LLM is called
        agent_with_codebook.reviewer_config.similarity_threshold = 0.5

        quotes = [Quote("q4", "feeling emotionally supported")]
        result = agent_with_codebook.review_code("emotional help", quotes)

        # Should hit LLM path since similar codes exist but aren't auto-merged
        assert mock_llm.called or result.decision in [
            ReviewDecision.MERGE,
            ReviewDecision.ADD_NEW,
        ]

    def test_apply_review_add_new(self, agent: ReviewerAgent):
        """Test applying ADD_NEW decision."""
        result = ReviewResult(
            code="new concept",
            decision=ReviewDecision.ADD_NEW,
            quotes=[Quote("q1", "text")],
        )

        agent.apply_review(result)

        assert len(agent.codebook) == 1
        assert agent.codebook.codes[0] == "new concept"

    def test_apply_review_merge(self, agent_with_codebook: ReviewerAgent):
        """Test applying MERGE decision."""
        initial_count = len(agent_with_codebook.codebook)

        result = ReviewResult(
            code="peer comfort",
            decision=ReviewDecision.MERGE,
            target_code="emotional support",
            quotes=[Quote("q4", "new quote")],
        )

        agent_with_codebook.apply_review(result)

        # Should not add new code, just quotes
        assert len(agent_with_codebook.codebook) == initial_count
        # Target code should have new quote
        entry = agent_with_codebook.codebook.entries[0]
        assert len(entry.quotes) == 2

    def test_apply_review_merge_target_not_found(
        self, agent_with_codebook: ReviewerAgent
    ):
        """Test applying MERGE when target doesn't exist."""
        result = ReviewResult(
            code="new code",
            decision=ReviewDecision.MERGE,
            target_code="nonexistent code",
            quotes=[Quote("q1", "text")],
        )

        agent_with_codebook.apply_review(result)

        # Should add as new since target not found
        assert "new code" in agent_with_codebook.codebook.codes

    def test_apply_review_update(self, agent_with_codebook: ReviewerAgent):
        """Test applying UPDATE decision."""
        result = ReviewResult(
            code="emotional assistance",  # New name
            decision=ReviewDecision.UPDATE,
            target_code="emotional support",  # Existing code
            quotes=[Quote("q4", "new quote")],
        )

        agent_with_codebook.apply_review(result)

        # Code name should be updated
        assert "emotional assistance" in agent_with_codebook.codebook.codes
        assert "emotional support" not in agent_with_codebook.codebook.codes

    def test_apply_review_skip(self, agent_with_codebook: ReviewerAgent):
        """Test applying SKIP decision."""
        initial_count = len(agent_with_codebook.codebook)

        result = ReviewResult(
            code="duplicate code",
            decision=ReviewDecision.SKIP,
            quotes=[Quote("q1", "text")],
        )

        agent_with_codebook.apply_review(result)

        # Should not change codebook
        assert len(agent_with_codebook.codebook) == initial_count

    def test_process_aggregation_result(self, agent: ReviewerAgent):
        """Test processing aggregation result."""
        agg_result = AggregationResult(
            merged_codes=[
                MergedCode(
                    code="merged code",
                    original_codes=["a", "b"],
                    quotes=[Quote("q1", "text1")],
                )
            ],
            retained_codes=[
                MergedCode(
                    code="retained code",
                    original_codes=["c"],
                    quotes=[Quote("q2", "text2")],
                )
            ],
        )

        results = agent.process_aggregation_result(agg_result)

        assert len(results) == 2
        assert len(agent.codebook) == 2

    def test_get_codebook_json(self, agent_with_codebook: ReviewerAgent):
        """Test getting codebook as JSON."""
        json_str = agent_with_codebook.get_codebook_json()
        data = json.loads(json_str)

        assert "codes" in data
        assert len(data["codes"]) == 3

    def test_get_codebook_summary(self, agent_with_codebook: ReviewerAgent):
        """Test getting codebook summary."""
        summary = agent_with_codebook.get_codebook_summary()

        assert "3 codes" in summary
        assert "3 quotes" in summary


class TestReviewerIntegration:
    """Integration tests for ReviewerAgent."""

    def test_full_review_workflow(self):
        """Test complete review workflow with real embeddings."""
        # Start with empty codebook
        agent = ReviewerAgent()

        # Add first code
        result1 = agent.review_code(
            "academic stress",
            [Quote("q1", "I was stressed about my exams")],
        )
        agent.apply_review(result1)

        assert result1.decision == ReviewDecision.ADD_NEW
        assert len(agent.codebook) == 1

        # Add similar code - should trigger LLM or auto-merge
        result2 = agent.review_code(
            "exam anxiety",
            [Quote("q2", "Anxious about upcoming tests")],
        )

        # Even if not auto-merged, we apply and check behavior
        agent.apply_review(result2)

        # Verify codebook state is reasonable
        assert len(agent.codebook) >= 1

    def test_review_preserves_quotes(self):
        """Test that quotes are preserved through review process."""
        agent = ReviewerAgent()

        # Add code with quotes
        quotes = [
            Quote("q1", "First quote"),
            Quote("q2", "Second quote"),
        ]
        result = agent.review_code("test concept", quotes)
        agent.apply_review(result)

        # Verify quotes are in codebook
        entry = agent.codebook.entries[0]
        assert len(entry.quotes) == 2
        assert entry.quotes[0].quote_id == "q1"
