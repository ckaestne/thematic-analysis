"""Tests for the Stage 1 adversarial refinement step.

Covers:
- three-call flow: code → critic (separate chat) → refine (coder chat continues)
- the critic chat has *only* segment text + codes (no codebook, no
  identity, no research context)
- caching-friendliness: the coder chat reuses its prefix for calls 1
  and 3
- empty first response (OUT_OF_SCOPE) short-circuits both critic and
  refinement calls
- async path mirrors sync behaviour
- worker integration: default_coder_factory wraps CoderAgent in
  RefiningCoderAgent
- end-to-end persistence picks up the refined codes
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
from thematic_analysis.research_context import ResearchContext
from thematic_analysis_inc import store, workers
from thematic_analysis_inc.refinement import (
    CRITIC_SYSTEM_PROMPT,
    Critic,
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
    return json.dumps(
        {"codes": codes, "rationales": rationales, "is_new": is_new}
    )


@dataclass
class _Call:
    messages: list[Message]
    response_format: Any


def _fake_llm(responses: list[str]) -> tuple[MagicMock, list[_Call]]:
    calls: list[_Call] = []
    iter_responses = iter(responses)

    def fake_completion(messages, **kwargs):
        calls.append(
            _Call(
                messages=list(messages),
                response_format=kwargs.get("response_format"),
            )
        )
        return _make_response(next(iter_responses))

    llm = MagicMock()
    llm.completion.side_effect = fake_completion
    return llm, calls


def _coder_with_llm(llm: MagicMock) -> CoderAgent:
    coder = CoderAgent(codebook=Codebook(use_mock_embeddings=True))
    coder._llm = llm
    return coder


class TestCritic:
    def test_critic_messages_contain_only_text_and_codes(self):
        llm, calls = _fake_llm(["these codes are shallow"])
        critic = Critic(llm)
        out = critic.critique(
            "The interviewee said X.",
            ["shallow", "another"],
            ["restates the line", "vague"],
        )

        assert out == "these codes are shallow"
        assert len(calls) == 1
        msgs = calls[0].messages
        assert [m.role for m in msgs] == ["system", "user"]
        assert msgs[0].content[0].text == CRITIC_SYSTEM_PROMPT

        user_text = msgs[1].content[0].text
        assert "The interviewee said X." in user_text
        assert "shallow" in user_text
        assert "another" in user_text
        assert "restates the line" in user_text
        # No coder scaffolding should leak in.
        assert "Current Codebook" not in user_text
        assert "Your Perspective" not in user_text
        assert "Research Context" not in user_text
        assert "Similar Existing Codes" not in user_text

    def test_critic_call_has_no_json_schema(self):
        """Critique is free-form prose, not JSON."""
        llm, calls = _fake_llm(["critique text"])
        Critic(llm).critique("text", ["c"], ["r"])
        assert calls[0].response_format is None

    @pytest.mark.asyncio
    async def test_critic_async(self):
        llm, calls = _fake_llm(["async critique"])
        out = await Critic(llm).critique_async("text", ["c"], ["r"])
        assert out == "async critique"
        assert len(calls) == 1


class TestRefiningCoderAgent:
    def test_three_call_flow_with_separate_critic_chat(self):
        first = _json_response(["shallow"], ["restates"], [True])
        critique = "These codes just paraphrase the segment. Sharpen them."
        refined = _json_response(["analytic-concept"], ["captures recurring concept"], [True])

        llm, calls = _fake_llm([first, critique, refined])
        coder = _coder_with_llm(llm)
        agent = RefiningCoderAgent(coder)

        result = agent.code_segment("seg_0001", "The interviewee said X.")

        assert result.codes == ["analytic-concept"]
        assert result.segment_id == "seg_0001"
        assert result.segment_text == "The interviewee said X."
        assert len(calls) == 3

        coder1, critic_call, coder2 = calls

        # Coder calls 1 and 3 share the prefix (cacheable).
        assert [m.role for m in coder1.messages] == ["system", "user"]
        assert [m.role for m in coder2.messages] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        assert coder2.messages[0] == coder1.messages[0]
        assert coder2.messages[1] == coder1.messages[1]
        assert coder2.messages[2].content[0].text == first

        # Critic chat is fully separate: own system prompt, no
        # leakage of coder context, no JSON schema enforcement.
        assert [m.role for m in critic_call.messages] == ["system", "user"]
        assert critic_call.messages[0].content[0].text == CRITIC_SYSTEM_PROMPT
        assert critic_call.response_format is None

        # The critique is fed back into the coder chat as the final user turn.
        refine_user_text = coder2.messages[3].content[0].text
        assert critique in refine_user_text
        assert "independent reviewer" in refine_user_text.lower()

        # Coder calls still use the JSON schema.
        assert coder1.response_format is not None
        assert coder2.response_format is not None
        assert coder1.response_format["type"] == "json_schema"
        assert coder2.response_format["type"] == "json_schema"

    def test_empty_first_pass_skips_critic_and_refinement(self):
        """OUT_OF_SCOPE: only the first call should be made."""
        llm, calls = _fake_llm([_json_response([], [], [])])
        coder = _coder_with_llm(llm)
        agent = RefiningCoderAgent(coder)

        result = agent.code_segment("seg_x", "off-topic chatter")

        assert result.codes == []
        assert len(calls) == 1

    def test_refined_response_replaces_first(self):
        llm, calls = _fake_llm(
            [
                _json_response(["A", "B", "C", "D"]),
                "merge A and B; C is fine; D is a paraphrase",
                _json_response(["merged-AB", "C"]),
            ]
        )
        agent = RefiningCoderAgent(_coder_with_llm(llm))
        result = agent.code_segment("seg_1", "text")
        assert result.codes == ["merged-AB", "C"]
        assert len(calls) == 3

    def test_critic_chat_does_not_see_codebook_or_identity(self):
        """Even with a fully-populated coder context, the critic chat
        must not receive it."""
        from thematic_analysis.agents.coder import CoderConfig
        from thematic_analysis.codebook import Quote

        codebook = Codebook(use_mock_embeddings=True)
        codebook.add_code("existing-code", [Quote("q1", "some quote")])
        coder = CoderAgent(
            config=CoderConfig(identity="feminist scholar"),
            codebook=codebook,
            research_context=ResearchContext(
                title="t", aim="study gendered narratives"
            ),
        )
        llm, calls = _fake_llm(
            [
                _json_response(["x"], ["r"], [True]),
                "weak code",
                _json_response(["x-refined"]),
            ]
        )
        coder._llm = llm

        agent = RefiningCoderAgent(coder)
        agent.code_segment("seg_1", "the actual segment text")

        critic_user = calls[1].messages[1].content[0].text
        assert "existing-code" not in critic_user
        assert "feminist scholar" not in critic_user
        assert "gendered narratives" not in critic_user
        assert "the actual segment text" in critic_user
        assert "x" in critic_user

    def test_research_context_passthrough(self):
        llm, _ = _fake_llm([_json_response([])])
        coder = _coder_with_llm(llm)
        agent = RefiningCoderAgent(coder)

        ctx = ResearchContext(title="t", aim="a")
        agent.research_context = ctx
        assert coder.research_context is ctx
        assert agent.research_context is ctx

    @pytest.mark.asyncio
    async def test_async_flow_mirrors_sync(self):
        llm, calls = _fake_llm(
            [
                _json_response(["shallow"], ["paraphrase"], [True]),
                "critique",
                _json_response(["deeper"], ["concept"], [True]),
            ]
        )
        agent = RefiningCoderAgent(_coder_with_llm(llm))
        result = await agent.code_segment_async("seg_1", "text")

        assert result.codes == ["deeper"]
        assert len(calls) == 3
        assert [m.role for m in calls[2].messages] == [
            "system",
            "user",
            "assistant",
            "user",
        ]

    def test_custom_critic_is_used(self):
        """If a Critic is supplied explicitly, it's used instead of the
        default. Lets users plug in a different LLM for the critic."""
        coder_llm, coder_calls = _fake_llm(
            [_json_response(["c"], ["r"], [True]), _json_response(["c'"])]
        )
        critic_llm, critic_calls = _fake_llm(["explicit critic spoke"])
        coder = _coder_with_llm(coder_llm)
        agent = RefiningCoderAgent(coder, critic=Critic(critic_llm))
        agent.code_segment("s", "t")

        # Coder LLM took two calls, critic LLM took one.
        assert len(coder_calls) == 2
        assert len(critic_calls) == 1
        # Critique text reached the refinement turn.
        refine_text = coder_calls[1].messages[3].content[0].text
        assert "explicit critic spoke" in refine_text


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
        assert agent.coder.config.identity == "x"


class TestWorkerEndToEnd:
    """workers.code_one drives RefiningCoderAgent and persists the
    *refined* codes, not the first pass."""

    def test_code_one_persists_refined_codes(self, tmp_path: Path):
        db = tmp_path / "x.sqlite"
        conn = store.init_db(db)
        store.add_coder(conn, "c1", "id1")
        store.enqueue_segments(conn, _segments(1))

        llm, _ = _fake_llm(
            [
                _json_response(["shallow"], ["paraphrase"], [True]),
                "the codes are too shallow",
                _json_response(["deeper-concept"], ["analytic"], [True]),
            ]
        )

        def factory(codebook, coder):
            base = CoderAgent(codebook=codebook)
            base.coder_config.identity = coder.identity
            base._llm = llm
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
