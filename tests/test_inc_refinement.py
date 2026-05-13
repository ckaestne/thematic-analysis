"""Tests for the Stage 1 refinement step (RefiningCoderAgent).

Covers:
- two-turn flow: first response + challenge prompt + refined response
- caching-friendliness: the prefix of the second call equals the messages
  of the first call followed by the assistant's first reply
- empty first response (OUT_OF_SCOPE) short-circuits — no second LLM call
- worker integration: default_coder_factory wraps CoderAgent in
  RefiningCoderAgent
- async path mirrors sync behaviour
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from openhands.sdk import Message, TextContent

from thematic_analysis.agents.coder import CoderAgent
from thematic_analysis.codebook import Codebook
from thematic_analysis_inc import store, workers
from thematic_analysis_inc.refinement import (
    REFINEMENT_USER_PROMPT,
    RefiningCoderAgent,
    wrap_with_refinement,
)


def _segments(n: int) -> list[tuple[str, str]]:
    return [(f"seg_{i:04d}", f"text {i}") for i in range(n)]


def _make_response(text: str) -> Any:
    """Mimic the SDK completion response shape used by BaseAgent."""
    resp = MagicMock()
    msg = MagicMock()
    msg.content = [TextContent(text=text)]
    resp.message = msg
    return resp


def _json_response(codes, rationales=None, is_new=None) -> str:
    rationales = rationales or [""] * len(codes)
    is_new = is_new if is_new is not None else [True] * len(codes)
    return json.dumps({"codes": codes, "rationales": rationales, "is_new": is_new})


@dataclass
class _Call:
    messages: list[Message]


def _make_coder_with_recorded_llm(responses: list[str]) -> tuple[CoderAgent, list[_Call]]:
    """Return a CoderAgent whose .llm.completion records each call and
    returns the next preset response text."""
    coder = CoderAgent(codebook=Codebook(use_mock_embeddings=True))
    calls: list[_Call] = []
    iter_responses = iter(responses)

    def fake_completion(messages, **kwargs):
        calls.append(_Call(messages=list(messages)))
        return _make_response(next(iter_responses))

    fake_llm = MagicMock()
    fake_llm.completion.side_effect = fake_completion
    coder._llm = fake_llm
    return coder, calls


class TestRefiningCoderAgent:
    def test_two_turn_flow_uses_shared_prefix(self):
        first = _json_response(["shallow code"], ["restates the line"], [True])
        refined = _json_response(
            ["analytic concept"], ["captures recurring concept"], [True]
        )
        coder, calls = _make_coder_with_recorded_llm([first, refined])

        agent = RefiningCoderAgent(coder)
        result = agent.code_segment("seg_0001", "The interviewee said X.")

        assert result.codes == ["analytic concept"]
        assert result.segment_id == "seg_0001"
        assert result.segment_text == "The interviewee said X."

        assert len(calls) == 2, "refinement must make a second LLM call"

        first_msgs = calls[0].messages
        second_msgs = calls[1].messages

        # Same prefix → cacheable.
        assert len(first_msgs) == 2
        assert first_msgs[0].role == "system"
        assert first_msgs[1].role == "user"
        assert len(second_msgs) == 4
        assert [m.role for m in second_msgs] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        assert second_msgs[0] == first_msgs[0]
        assert second_msgs[1] == first_msgs[1]

        # Assistant turn carries the first response verbatim.
        assistant_text = second_msgs[2].content[0].text
        assert assistant_text == first

        # Final user turn is the challenge prompt.
        challenge_text = second_msgs[3].content[0].text
        assert challenge_text == REFINEMENT_USER_PROMPT

    def test_empty_first_pass_skips_refinement(self):
        """OUT_OF_SCOPE-style empty assignment must not trigger another LLM call."""
        first = _json_response([], [], [])
        coder, calls = _make_coder_with_recorded_llm([first])

        agent = RefiningCoderAgent(coder)
        result = agent.code_segment("seg_x", "off-topic chatter")

        assert result.codes == []
        assert len(calls) == 1

    def test_refined_response_replaces_first(self):
        first = _json_response(["A", "B", "C", "D"])
        refined = _json_response(["merged-AB", "C"])
        coder, calls = _make_coder_with_recorded_llm([first, refined])

        agent = RefiningCoderAgent(coder)
        result = agent.code_segment("seg_1", "text")

        assert result.codes == ["merged-AB", "C"]
        assert len(calls) == 2

    def test_refinement_response_uses_coder_schema(self):
        coder, calls = _make_coder_with_recorded_llm(
            [_json_response(["x"]), _json_response(["x-refined"])]
        )
        agent = RefiningCoderAgent(coder)
        agent.code_segment("seg_1", "text")

        # Both LLM calls go through with the CoderAgent's response_format.
        for call in coder._llm.completion.call_args_list:
            kwargs = call.kwargs
            assert "response_format" in kwargs
            assert kwargs["response_format"]["type"] == "json_schema"

    def test_research_context_passthrough(self):
        from thematic_analysis.research_context import ResearchContext

        coder, _ = _make_coder_with_recorded_llm([_json_response([])])
        agent = RefiningCoderAgent(coder)

        ctx = ResearchContext(title="t", aim="a")
        agent.research_context = ctx
        assert coder.research_context is ctx
        assert agent.research_context is ctx

    @pytest.mark.asyncio
    async def test_async_flow_mirrors_sync(self):
        first = _json_response(["shallow"], ["paraphrase"], [True])
        refined = _json_response(["deeper"], ["concept"], [True])
        coder, calls = _make_coder_with_recorded_llm([first, refined])

        agent = RefiningCoderAgent(coder)
        result = await agent.code_segment_async("seg_1", "text")

        assert result.codes == ["deeper"]
        assert len(calls) == 2
        assert [m.role for m in calls[1].messages] == [
            "system",
            "user",
            "assistant",
            "user",
        ]


class TestWrapWithRefinement:
    def test_wraps_coder_agent(self):
        coder = CoderAgent(codebook=Codebook(use_mock_embeddings=True))
        wrapped = wrap_with_refinement(coder)
        assert isinstance(wrapped, RefiningCoderAgent)
        assert wrapped.coder is coder

    def test_passes_through_non_coder(self):
        sentinel = object()
        assert wrap_with_refinement(sentinel) is sentinel


class TestWorkerDefaultFactory:
    def test_default_factory_returns_refining_agent(self):
        codebook = Codebook(use_mock_embeddings=True)
        coder_row = store.Coder(coder_id="c1", identity="x", created_at="now")
        agent = workers.default_coder_factory(codebook, coder_row)
        assert isinstance(agent, RefiningCoderAgent)
        assert isinstance(agent.coder, CoderAgent)
        # Identity flows into the wrapped agent.
        assert agent.coder.config.identity == "x"


class TestWorkerEndToEnd:
    """End-to-end: workers.code_one drives RefiningCoderAgent and persists
    the *refined* codes, not the first pass."""

    def test_code_one_persists_refined_codes(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "x.sqlite"
        conn = store.init_db(db)
        store.add_coder(conn, "c1", "id1")
        store.enqueue_segments(conn, _segments(1))

        def fake_completion_factory():
            responses = iter(
                [
                    _json_response(["shallow"], ["paraphrase"], [True]),
                    _json_response(["deeper-concept"], ["analytic"], [True]),
                ]
            )

            def fake_completion(messages, **kwargs):
                return _make_response(next(responses))

            return fake_completion

        def factory(codebook, coder):
            base = CoderAgent(
                codebook=codebook,
            )
            base.coder_config.identity = coder.identity
            fake_llm = MagicMock()
            fake_llm.completion.side_effect = fake_completion_factory()
            base._llm = fake_llm
            return wrap_with_refinement(base)

        res = workers.code_one(
            conn, "c1", use_mock_embeddings=True, agent_factory=factory
        )
        assert res is not None and res["ok"], res
        assert res["n_codes"] == 1

        codes = conn.execute(
            "SELECT code FROM coder_codes ORDER BY position"
        ).fetchall()
        assert [c["code"] for c in codes] == ["deeper-concept"]
