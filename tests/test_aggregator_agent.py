"""Tests for CodeAggregatorAgent."""

import json
from unittest.mock import patch

import pytest

from thematic_analysis.agents import AggregatorConfig, CodeAggregatorAgent
from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID
from thematic_analysis_inc.db.models import (
    Code,
    Codebook,
    Quote,
    SENTINEL_CODE_LABEL,
    Segment,
)


_next_qid = [0]


def _quote(text: str) -> Quote:
    _next_qid[0] += 1
    return Quote(quote_id=_next_qid[0], segment_id=1, text=text)


def _code(
    label: str,
    coder_id: int,
    quote_texts: list[str],
    *,
    code_id: int | None = None,
    codebook_used_id: int = 1,
) -> Code:
    c = Code(
        code_id=code_id,
        segment_id=1,
        coder_id=coder_id,
        codebook_used_id=codebook_used_id,
        code=label,
        description=f"desc:{label}",
        rationale="",
    )
    c.supporting_quotes = [_quote(t) for t in quote_texts]
    return c


def _make_segment(codes: list[Code]) -> tuple[Segment, Codebook]:
    cb = Codebook(version=1, research_context_version=1)
    seg = Segment(
        segment_id=1,
        document_id=1,
        content="seg",
        line_from=1,
        line_to=1,
        position=0,
    )
    seg.codes = codes
    for c in codes:
        if c.codebook_used_id == cb.version:
            c.codebook_used = cb
    return seg, cb


class TestAggregatorConfig:
    def test_default_config(self):
        assert AggregatorConfig().max_quotes_per_code == 10

    def test_custom_config(self):
        assert AggregatorConfig(max_quotes_per_code=5).max_quotes_per_code == 5


class TestCodeAggregatorAgent:
    @pytest.fixture
    def agent(self) -> CodeAggregatorAgent:
        return CodeAggregatorAgent()

    @pytest.fixture
    def sample_codes(self) -> list[Code]:
        return [
            _code("peer support", 1, ["I felt really supported by my friends"]),
            _code("emotional comfort", 1, ["I felt really supported by my friends"]),
            _code("peer support", 2, ["My classmates helped me through it"]),
            _code("academic help", 2, ["My classmates helped me through it"]),
            _code("time pressure", 3, ["Time pressure was overwhelming"]),
        ]

    def test_get_system_prompt(self, agent: CodeAggregatorAgent):
        prompt = agent.get_system_prompt()
        assert "aggregator coder" in prompt
        assert "merge_groups" in prompt
        assert "retain_code_ids" in prompt
        assert "same coder" in prompt

    def test_build_prompt_payload_assigns_sequential_ids(
        self, agent: CodeAggregatorAgent, sample_codes: list[Code]
    ):
        grouped: dict[int, list[Code]] = {}
        for c in sample_codes:
            grouped.setdefault(c.coder_id, []).append(c)
        payload, code_index = agent._build_prompt_payload(
            [grouped[k] for k in sorted(grouped)]
        )
        assert sorted(code_index.keys()) == [1, 2, 3, 4, 5]
        coders = payload["coders"]
        assert [c["coder_id"] for c in coders] == [1, 2, 3]
        assert [c["label"] for c in coders[0]["codes"]] == [
            "peer support",
            "emotional comfort",
        ]

    def test_build_prompt_payload_full_quote_text(
        self, agent: CodeAggregatorAgent
    ):
        long_text = "x" * 500
        payload, _ = agent._build_prompt_payload([[_code("c", 1, [long_text])]])
        assert payload["coders"][0]["codes"][0]["quotes"][0] == long_text

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_merges_and_retains(
        self,
        mock_llm,
        agent: CodeAggregatorAgent,
        sample_codes: list[Code],
    ):
        # Pin code_ids so we can tell new vs. retained apart in the result.
        for i, c in enumerate(sample_codes, start=100):
            c.code_id = i
        seg, cb = _make_segment(sample_codes)
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
        result = agent.aggregate(seg, cb)
        # 1 new merged code + 3 retained originals.
        new_codes = [c for c in result if c.code_id is None]
        retained = [c for c in result if c.code_id is not None]
        assert len(new_codes) == 1
        assert new_codes[0].code == "peer support system"
        assert new_codes[0].coder_id == SYSTEM_AGGREGATOR_ID
        assert new_codes[0].codebook_used_id == cb.version
        assert {q.text for q in new_codes[0].supporting_quotes} == {
            "I felt really supported by my friends",
            "My classmates helped me through it",
        }
        # Derivation edges point at the two merged source codes.
        assert sorted(
            e.source_code.code_id for e in new_codes[0].derivation_sources
        ) == sorted(
            c.code_id for c in sample_codes if c.code in {"peer support"}
        )
        assert {c.code for c in retained} == {
            "emotional comfort",
            "academic help",
            "time pressure",
        }
        mock_llm.assert_called_once()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_empty_input_returns_empty(
        self, mock_llm, agent: CodeAggregatorAgent
    ):
        seg, cb = _make_segment([])
        assert agent.aggregate(seg, cb) == []
        mock_llm.assert_not_called()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_all_sentinels_returns_sentinel(
        self, mock_llm, agent: CodeAggregatorAgent
    ):
        seg, cb = _make_segment(
            [
                _code(SENTINEL_CODE_LABEL, 1, []),
                _code(SENTINEL_CODE_LABEL, 2, []),
            ]
        )
        result = agent.aggregate(seg, cb)
        assert len(result) == 1
        assert result[0].code == SENTINEL_CODE_LABEL
        assert result[0].coder_id == SYSTEM_AGGREGATOR_ID
        mock_llm.assert_not_called()

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_single_coder_bypasses_llm(
        self, mock_llm, agent: CodeAggregatorAgent
    ):
        codes = [
            _code("a", 1, ["q1"], code_id=10),
            _code("b", 1, ["q2"], code_id=11),
        ]
        seg, cb = _make_segment(codes)
        result = agent.aggregate(seg, cb)
        mock_llm.assert_not_called()
        # Two new aggregator codes (coder_id=0), one per input.
        assert all(c.code_id is None for c in result)
        assert [c.code for c in result] == ["a", "b"]
        assert all(c.coder_id == SYSTEM_AGGREGATOR_ID for c in result)
        # Each new code keeps the source's quotes and a single edge back.
        for new, src in zip(result, codes):
            assert [q.text for q in new.supporting_quotes] == [
                q.text for q in src.supporting_quotes
            ]
            assert len(new.derivation_sources) == 1
            assert new.derivation_sources[0].source_code is src

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_drops_sentinels_when_real_codes_exist(
        self, mock_llm, agent: CodeAggregatorAgent
    ):
        codes = [
            _code(SENTINEL_CODE_LABEL, 1, []),
            _code("real", 2, ["q"], code_id=42),
            _code("real", 3, ["r"], code_id=43),
        ]
        seg, cb = _make_segment(codes)
        mock_llm.return_value = json.dumps(
            {"merge_groups": [], "retain_code_ids": [1, 2]}
        )
        result = agent.aggregate(seg, cb)
        # The sentinel is dropped; the two real codes are retained as-is.
        assert {c.code_id for c in result} == {42, 43}

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_ignores_other_codebook_versions(
        self, mock_llm, agent: CodeAggregatorAgent
    ):
        codes = [
            _code("a", 1, ["q1"], codebook_used_id=1, code_id=1),
            _code("a2", 2, ["q2"], codebook_used_id=1, code_id=2),
            _code("b", 1, ["q3"], codebook_used_id=2, code_id=3),
        ]
        seg, cb = _make_segment(codes)
        mock_llm.return_value = json.dumps(
            {"merge_groups": [], "retain_code_ids": [1, 2]}
        )
        result = agent.aggregate(seg, cb)
        # Only codes at codebook v1 are considered; cb-v2 code is ignored.
        assert {c.code_id for c in result} == {1, 2}

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_without_codebook_runs_each_version(
        self, mock_llm, agent: CodeAggregatorAgent
    ):
        # Single-coder per codebook → the agent short-circuits past the
        # LLM but still emits one aggregator code per input. We get two
        # codebook invocations, two aggregator codes, no LLM calls.
        cb1 = Codebook(version=1, research_context_version=1)
        cb2 = Codebook(version=2, research_context_version=1)
        codes = [
            _code("a", 1, ["q1"], codebook_used_id=1, code_id=1),
            _code("b", 2, ["q2"], codebook_used_id=2, code_id=2),
        ]
        for c in codes:
            c.codebook_used = cb1 if c.codebook_used_id == 1 else cb2
        seg, _ = _make_segment(codes)
        result = agent.aggregate(seg)
        mock_llm.assert_not_called()
        assert len(result) == 2
        assert {c.code for c in result} == {"a", "b"}
        assert all(c.coder_id == SYSTEM_AGGREGATOR_ID for c in result)

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_ignores_coder_id_below_one(
        self, mock_llm, agent: CodeAggregatorAgent
    ):
        # An aggregator (coder_id=0) or reviewer (coder_id=-1) code from a
        # previous pass must not feed back into a new aggregation. With
        # two real coders below the LLM still runs.
        codes = [
            _code("prior-agg", 0, ["q"], code_id=10),
            _code("real-a", 2, ["q1"], code_id=20),
            _code("real-b", 3, ["q2"], code_id=21),
        ]
        seg, cb = _make_segment(codes)
        mock_llm.return_value = json.dumps(
            {"merge_groups": [], "retain_code_ids": [1, 2]}
        )
        result = agent.aggregate(seg, cb)
        assert {c.code_id for c in result} == {20, 21}

    @patch.object(CodeAggregatorAgent, "_call_llm")
    def test_aggregate_fallback_on_parse_error_retains_inputs(
        self,
        mock_llm,
        agent: CodeAggregatorAgent,
        sample_codes: list[Code],
    ):
        for i, c in enumerate(sample_codes, start=200):
            c.code_id = i
        seg, cb = _make_segment(sample_codes)
        mock_llm.return_value = "Invalid response"
        result = agent.aggregate(seg, cb)
        assert {c.code_id for c in result} == {c.code_id for c in sample_codes}

    def test_max_quotes_per_code_caps_merged_quotes(self):
        agent = CodeAggregatorAgent(config=AggregatorConfig(max_quotes_per_code=2))
        codes = [
            _code("a", 1, [f"q{i}" for i in range(3)], code_id=1),
            _code("b", 2, [f"q{i}" for i in range(3, 6)], code_id=2),
        ]
        seg, cb = _make_segment(codes)
        with patch.object(CodeAggregatorAgent, "_call_llm") as mock_llm:
            mock_llm.return_value = json.dumps(
                {
                    "merge_groups": [
                        {
                            "merged_code": "ab",
                            "original_code_ids": [1, 2],
                            "rationale": "",
                        }
                    ],
                    "retain_code_ids": [],
                }
            )
            result = agent.aggregate(seg, cb)
        new_codes = [c for c in result if c.code_id is None]
        assert len(new_codes[0].supporting_quotes) == 2
