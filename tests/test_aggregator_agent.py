"""Tests for CodeAggregatorAgent."""

import json
from unittest.mock import patch

import pytest

from thematic_analysis.agents import (
    AggregationResult,
    AggregatorConfig,
    CodeAggregatorAgent,
    MergedCode,
)
from thematic_analysis.codebook import Codebook, Quote
from thematic_analysis_inc.db.models import Code as DBCode, Quote as DBQuote


def _code(label: str, segment_id: int, quote_texts: list[str]) -> DBCode:
    c = DBCode(code=label, description=f"desc:{label}", segment_id=segment_id)
    c.supporting_quotes = [DBQuote(text=t, segment_id=segment_id) for t in quote_texts]
    return c


class TestAggregatorConfig:
    def test_default_config(self):
        config = AggregatorConfig()
        assert config.similarity_threshold == 0.8
        assert config.max_quotes_per_code == 10

    def test_custom_config(self):
        config = AggregatorConfig(
            similarity_threshold=0.9,
            max_quotes_per_code=5,
        )
        assert config.similarity_threshold == 0.9
        assert config.max_quotes_per_code == 5


class TestMergedCode:
    def test_merged_code_creation(self):
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
    def test_aggregation_result_creation(self):
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
        merged = MergedCode("m1", ["a"], [])
        retained = MergedCode("r1", ["b"], [])
        result = AggregationResult(
            merged_codes=[merged],
            retained_codes=[retained],
        )
        assert len(result.all_codes()) == 2


class TestCodeAggregatorAgent:
    @pytest.fixture
    def agent(self) -> CodeAggregatorAgent:
        return CodeAggregatorAgent()

    @pytest.fixture
    def sample_coder_codes(self) -> list[list[DBCode]]:
        """Per-coder lists of Code rows for one (notional) segment."""
        return [
            [
                _code("peer support", 1, ["I felt really supported by my friends"]),
                _code("emotional comfort", 1, ["I felt really supported by my friends"]),
            ],
            [
                _code("peer support", 2, ["My classmates helped me through it"]),
                _code("academic help", 2, ["My classmates helped me through it"]),
            ],
            [
                _code("time pressure", 3, ["Time pressure was overwhelming"]),
            ],
        ]

    def test_initialization(self, agent: CodeAggregatorAgent):
        assert agent.codebook is not None
        assert isinstance(agent.aggregator_config, AggregatorConfig)

    def test_get_system_prompt(self, agent: CodeAggregatorAgent):
        prompt = agent.get_system_prompt()
        assert "qualitative researcher" in prompt
        assert "merge_groups" in prompt
        assert "retain_codes" in prompt

    def test_collect_codes_with_quotes(
        self,
        agent: CodeAggregatorAgent,
        sample_coder_codes: list[list[DBCode]],
    ):
        code_quotes = agent._collect_codes_with_quotes(sample_coder_codes)
        assert "peer support" in code_quotes
        # Two distinct quote texts across the two coders.
        assert len(code_quotes["peer support"]) == 2
        assert "emotional comfort" in code_quotes
        assert "time pressure" in code_quotes

    def test_collect_codes_dedupes_quote_text(self, agent: CodeAggregatorAgent):
        # Two coders producing the same code with the same quote text.
        coder_codes = [
            [_code("c1", 1, ["same quote"])],
            [_code("c1", 1, ["same quote"])],
        ]
        code_quotes = agent._collect_codes_with_quotes(coder_codes)
        assert len(code_quotes["c1"]) == 1

    def test_find_similar_groups_single_code(self, agent: CodeAggregatorAgent):
        groups = agent._find_similar_groups(["single code"])
        assert groups == [["single code"]]

    def test_find_similar_groups_empty(self, agent: CodeAggregatorAgent):
        assert agent._find_similar_groups([]) == []

    def test_format_codes_section(self, agent: CodeAggregatorAgent):
        code_quotes = {
            "test code": [Quote("q1", "sample quote text")],
        }
        section = agent._format_codes_section(code_quotes)
        assert "test code" in section
        assert "1 quotes" in section

    def test_format_similar_groups_section(self, agent: CodeAggregatorAgent):
        groups = [["code1", "code2"], ["standalone"]]
        section = agent._format_similar_groups_section(groups)
        assert "Group 1:" in section
        assert "code1, code2" in section
        assert "Standalone:" in section

    def test_format_similar_groups_empty(self, agent: CodeAggregatorAgent):
        assert "No similar groups" in agent._format_similar_groups_section([])

    def test_parse_response_valid_json(self, agent: CodeAggregatorAgent):
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
        code_quotes = {"code1": [Quote("q1", "text")]}
        response = '{"merge_groups": [], "retain_codes": ["code1"]}'
        result = agent._parse_response(response, code_quotes)
        assert result is not None
        assert len(result.retained_codes) == 1

    def test_parse_response_invalid_json(self, agent: CodeAggregatorAgent):
        assert agent._parse_response("Not JSON", {}) is None

    def test_parse_response_respects_max_quotes(self):
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
        sample_coder_codes: list[list[DBCode]],
    ):
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
        result = agent.aggregate(sample_coder_codes, apply_negotiation=False)
        assert len(result.merged_codes) == 1
        assert result.merged_codes[0].code == "peer support system"
        assert len(result.retained_codes) == 2
        mock_llm.assert_called_once()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_with_negotiation(
        self,
        mock_llm,
        agent: CodeAggregatorAgent,
        sample_coder_codes: list[list[DBCode]],
    ):
        """With CONSENSUS, only 'peer support' (2/3 coders) is kept."""
        mock_llm.return_value = json.dumps(
            {
                "merge_groups": [],
                "retain_codes": ["peer support"],
            }
        )
        result = agent.aggregate(sample_coder_codes, apply_negotiation=True)
        assert len(result.merged_codes) == 0
        assert len(result.retained_codes) == 1
        assert result.retained_codes[0].code == "peer support"
        mock_llm.assert_called_once()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_empty_input(self, mock_llm, agent: CodeAggregatorAgent):
        result = agent.aggregate([])
        assert len(result.merged_codes) == 0
        assert len(result.retained_codes) == 0
        mock_llm.assert_not_called()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_fallback_on_parse_error(
        self,
        mock_llm,
        agent: CodeAggregatorAgent,
        sample_coder_codes: list[list[DBCode]],
    ):
        mock_llm.return_value = "Invalid response"
        result = agent.aggregate(sample_coder_codes)
        # Should return all codes as retained
        assert len(result.merged_codes) == 0
        assert len(result.retained_codes) > 0

    def test_update_codebook(self, agent: CodeAggregatorAgent):
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
        codebook.add_code("test", [Quote("q1", "test")])

        agent = CodeAggregatorAgent(codebook=codebook)
        groups = agent._find_similar_groups(
            ["peer support", "friend assistance", "time pressure"]
        )
        assert len(groups) >= 1

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_full_aggregation_workflow(self, mock_llm):
        codebook = Codebook()
        codebook.add_code("test", [Quote("q1", "test")])

        agent = CodeAggregatorAgent(codebook=codebook)
        coder_codes = [
            [_code("peer support", 1, ["supported"])],
            [_code("friend help", 2, ["helped me"])],
        ]
        mock_llm.return_value = json.dumps(
            {
                "merge_groups": [
                    {
                        "merged_code": "peer support",
                        "original_codes": ["peer support", "friend help"],
                        "rationale": "Same concept",
                    }
                ],
                "retain_codes": [],
            }
        )
        result = agent.aggregate(coder_codes, apply_negotiation=False)
        assert len(result.merged_codes) == 1
