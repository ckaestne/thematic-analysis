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
from thematic_analysis.codebook import Quote
from thematic_analysis_inc.db.models import Code as DBCode, Quote as DBQuote


def _code(label: str, coder_id: int, quote_texts: list[str]) -> DBCode:
    c = DBCode(
        code=label,
        description=f"desc:{label}",
        segment_id=1,
        coder_id=coder_id,
    )
    c.supporting_quotes = [DBQuote(text=t, segment_id=1) for t in quote_texts]
    return c


class TestAggregatorConfig:
    def test_default_config(self):
        config = AggregatorConfig()
        assert config.max_quotes_per_code == 10

    def test_custom_config(self):
        config = AggregatorConfig(max_quotes_per_code=5)
        assert config.max_quotes_per_code == 5


class TestMergedCode:
    def test_merged_code_creation(self):
        quotes = [Quote("1", "text1"), Quote("2", "text2")]
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
    def test_to_json(self):
        merged = MergedCode(
            code="merged",
            original_codes=["a", "b"],
            quotes=[Quote("1", "text1")],
            merge_rationale="Same concept",
        )
        result = AggregationResult(merged_codes=[merged], retained_codes=[])
        data = json.loads(result.to_json())
        assert data["merged_codes"][0]["code"] == "merged"
        assert data["merged_codes"][0]["merge_rationale"] == "Same concept"

    def test_all_codes(self):
        merged = MergedCode("m1", ["a"], [])
        retained = MergedCode("r1", ["b"], [])
        result = AggregationResult(merged_codes=[merged], retained_codes=[retained])
        assert len(result.all_codes()) == 2


class TestCodeAggregatorAgent:
    @pytest.fixture
    def agent(self) -> CodeAggregatorAgent:
        return CodeAggregatorAgent()

    @pytest.fixture
    def sample_coder_codes(self) -> list[list[DBCode]]:
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
        assert isinstance(agent.aggregator_config, AggregatorConfig)

    def test_get_system_prompt(self, agent: CodeAggregatorAgent):
        prompt = agent.get_system_prompt()
        assert "aggregator coder" in prompt
        assert "merge_groups" in prompt
        assert "retain_code_ids" in prompt
        # Codes from the same coder must not be merged with each other.
        assert "same coder" in prompt

    def test_build_prompt_payload_assigns_sequential_ids(
        self,
        agent: CodeAggregatorAgent,
        sample_coder_codes: list[list[DBCode]],
    ):
        payload, code_index = agent._build_prompt_payload(sample_coder_codes)

        # Codes are numbered 1..5 (2 + 2 + 1), one per input code.
        assert sorted(code_index.keys()) == [1, 2, 3, 4, 5]
        # Coder attribution preserved.
        coders = payload["coders"]
        assert [c["coder_id"] for c in coders] == [1, 2, 3]
        assert [c["label"] for c in coders[0]["codes"]] == [
            "peer support",
            "emotional comfort",
        ]
        # Quote texts are inlined per code; collect across all codes.
        all_quote_texts: set[str] = set()
        for c in coders:
            for code in c["codes"]:
                all_quote_texts.update(code["quotes"])
        assert all_quote_texts == {
            "I felt really supported by my friends",
            "My classmates helped me through it",
            "Time pressure was overwhelming",
        }
        # Both codes from coder 1 carry the same supporting quote text.
        quotes_coder1 = {tuple(c["quotes"]) for c in coders[0]["codes"]}
        assert quotes_coder1 == {tuple(coders[0]["codes"][0]["quotes"])}

    def test_build_prompt_payload_full_quote_text(self, agent: CodeAggregatorAgent):
        long_text = "x" * 500
        coder_codes = [[_code("c", 1, [long_text])]]
        payload, _ = agent._build_prompt_payload(coder_codes)
        assert payload["coders"][0]["codes"][0]["quotes"][0] == long_text  # not truncated

    def test_parse_response_merges_by_id(
        self,
        agent: CodeAggregatorAgent,
        sample_coder_codes: list[list[DBCode]],
    ):
        _, code_index = agent._build_prompt_payload(sample_coder_codes)
        response = json.dumps(
            {
                "merge_groups": [
                    {
                        "merged_code": "peer support system",
                        "original_code_ids": [1, 3],
                        "rationale": "across coders",
                    }
                ],
                "retain_code_ids": [2, 4, 5],
            }
        )
        result = agent._parse_response(response, code_index)
        assert result is not None
        assert len(result.merged_codes) == 1
        assert result.merged_codes[0].code == "peer support system"
        assert set(result.merged_codes[0].original_codes) == {"peer support"}
        assert len(result.merged_codes[0].quotes) == 2  # one per source row
        labels = {rc.code for rc in result.retained_codes}
        assert labels == {"emotional comfort", "academic help", "time pressure"}

    def test_parse_response_unknown_ids_ignored(
        self, agent: CodeAggregatorAgent
    ):
        coder_codes = [[_code("c1", 1, ["t"])]]
        _, code_index = agent._build_prompt_payload(coder_codes)
        response = json.dumps(
            {
                "merge_groups": [
                    {
                        "merged_code": "ignored",
                        "original_code_ids": [999, 1000],
                        "rationale": "n/a",
                    }
                ],
                "retain_code_ids": [1, 9999],
            }
        )
        result = agent._parse_response(response, code_index)
        assert result is not None
        assert result.merged_codes == []  # all originals unknown → group dropped
        assert [rc.code for rc in result.retained_codes] == ["c1"]

    def test_parse_response_respects_max_quotes(self):
        config = AggregatorConfig(max_quotes_per_code=2)
        agent = CodeAggregatorAgent(config=config)
        coder_codes = [[_code("c1", 1, [f"q{i}" for i in range(5)])]]
        _, code_index = agent._build_prompt_payload(coder_codes)
        response = '{"merge_groups": [], "retain_code_ids": [1]}'
        result = agent._parse_response(response, code_index)
        assert result is not None
        assert len(result.retained_codes[0].quotes) == 2

    def test_parse_response_invalid_json(self, agent: CodeAggregatorAgent):
        assert agent._parse_response("Not JSON", {}) is None

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
                        "original_code_ids": [1, 3],
                        "rationale": "cross-coder match",
                    }
                ],
                "retain_code_ids": [2, 4, 5],
            }
        )
        result = agent.aggregate(sample_coder_codes)
        assert len(result.merged_codes) == 1
        assert result.merged_codes[0].code == "peer support system"
        assert len(result.retained_codes) == 3
        mock_llm.assert_called_once()
        # The user prompt is a JSON dump of the payload.
        user_prompt = mock_llm.call_args[0][1]
        payload = json.loads(user_prompt)
        assert "coders" in payload
        assert "quotes" in payload["coders"][0]["codes"][0]

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_empty_input(self, mock_llm, agent: CodeAggregatorAgent):
        result = agent.aggregate([])
        assert result.merged_codes == []
        assert result.retained_codes == []
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
        assert result.merged_codes == []
        assert len(result.retained_codes) > 0
