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
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from openhands.sdk import Message, TextContent

from thematic_analysis.agents.coder import CoderAgent
from thematic_analysis_inc.db.models import is_sentinel_code
from thematic_analysis.research_context import ResearchContext
from thematic_analysis_inc import workers
from thematic_analysis_inc import db as store
from thematic_analysis_inc.db.models import Code, Quote
from thematic_analysis_inc.refinement import (
    CRITIC_SYSTEM_PROMPT,
    Critic,
    RefiningCoderAgent,
    wrap_with_refinement,
)


# The segment text used across tests; quotes must be substrings of this.
SEG_TEXT = "the actual segment text we feed the coder"


def _seg(text: str, sid: int = 1):
    return SimpleNamespace(segment_id=sid, content=text, quotes=[])


def _coder_stub(coder_id: int = 1, identity: str | None = None):
    return SimpleNamespace(coder_id=coder_id, identity=identity)


def _rc_row(description: str = "", coder_prompt: str | None = None):
    return SimpleNamespace(
        description=description,
        coder_prompt=coder_prompt,
        coding_critic_prompt=None,
        reviewer_prompt=None,
    )


def _codebook_stub(version: int = 1, codes=None, research_context=None):
    return SimpleNamespace(
        version=version,
        codes=codes or [],
        research_context=research_context,
    )


def _make_response(text: str) -> Any:
    resp = MagicMock()
    msg = MagicMock()
    msg.content = [TextContent(text=text)]
    resp.message = msg
    return resp


def _json_codes(items: list[tuple[str, str, list[str]]]) -> str:
    """Build a JSON response payload. Each item is (code, description, quotes)."""
    return json.dumps(
        {
            "codes": [
                {"code": c, "description": d, "quotes": q} for c, d, q in items
            ]
        }
    )


def _single(label: str, quote: str = "actual segment") -> str:
    return _json_codes([(label, f"desc of {label}", [quote])])


def _make_code(label: str, quote: str) -> Code:
    c = Code(code=label, description=f"desc of {label}")
    c.supporting_quotes = [Quote(text=quote)]
    return c


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


def _coder_with_llm(
    llm: MagicMock,
    *,
    coder=None,
    codebook=None,
) -> CoderAgent:
    agent = CoderAgent(
        coder=coder or _coder_stub(),
        codebook=codebook or _codebook_stub(),
    )
    agent._llm = llm
    return agent


class TestCritic:
    def test_critic_messages_contain_only_text_and_codes(self):
        llm, calls = _fake_llm(["these codes are shallow"])
        critic = Critic(llm)
        codes = [_make_code("shallow", "interviewee said X")]
        out = critic.critique("The interviewee said X.", codes)

        assert out == "these codes are shallow"
        assert len(calls) == 1
        msgs = calls[0].messages
        assert [m.role for m in msgs] == ["system", "user"]
        assert msgs[0].content[0].text == CRITIC_SYSTEM_PROMPT

        user_text = msgs[1].content[0].text
        assert "The interviewee said X." in user_text
        assert "shallow" in user_text
        assert "interviewee said X" in user_text
        # No coder scaffolding should leak into the user message.
        assert "Current Codebook" not in user_text
        assert "Your Perspective" not in user_text
        assert "Research Context" not in user_text
        assert "Similar Existing Codes" not in user_text

    def test_critic_system_prompt_includes_research_context(self):
        llm, calls = _fake_llm(["critique"])
        ctx = ResearchContext(
            description=(
                "Climate skepticism. Understand rhetorical strategies of "
                "inaction. Research question: What rhetorical moves "
                "justify inaction?"
            ),
        )
        Critic(llm, research_context=ctx).critique(
            "text", [_make_code("c", "text")]
        )
        sys_prompt = calls[0].messages[0].content[0].text
        assert sys_prompt.startswith(CRITIC_SYSTEM_PROMPT)
        assert "## Research Context" in sys_prompt
        assert "Climate skepticism" in sys_prompt
        assert "rhetorical strategies of inaction" in sys_prompt
        assert "rhetorical moves justify inaction" in sys_prompt
        assert "relevant" in sys_prompt.lower()

    def test_critic_ignores_empty_research_context(self):
        llm, calls = _fake_llm(["critique"])
        Critic(llm, research_context=ResearchContext()).critique(
            "text", [_make_code("c", "text")]
        )
        assert calls[0].messages[0].content[0].text == CRITIC_SYSTEM_PROMPT

    def test_critic_call_has_no_json_schema(self):
        llm, calls = _fake_llm(["critique text"])
        Critic(llm).critique("text", [_make_code("c", "text")])
        assert calls[0].response_format is None

    def test_critic_prompt_emphasises_relevance_and_no_escape(self):
        import re

        flat = re.sub(r"\s+", " ", CRITIC_SYSTEM_PROMPT.lower())
        assert "research focus" in flat
        assert "off-topic" in flat
        assert "drop" in flat
        assert "uncoded" in flat
        assert "not to ratify them" in flat
        assert "conflation" not in flat
        assert "vagueness" not in flat

    @pytest.mark.asyncio
    async def test_critic_async(self):
        llm, calls = _fake_llm(["async critique"])
        out = await Critic(llm).critique_async(
            "text", [_make_code("c", "text")]
        )
        assert out == "async critique"
        assert len(calls) == 1


class TestRefiningCoderAgent:
    def test_refinement_prompt_asks_for_improvement_no_keep_unchanged(self):
        from thematic_analysis_inc.refinement import (
            _build_refinement_user_prompt,
        )

        text = _build_refinement_user_prompt("some critique").lower()
        assert "stronger" in text
        assert "keep your original" not in text
        assert "empty" in text and "valid" in text

    def test_three_call_flow_with_separate_critic_chat(self):
        first = _single("shallow", "actual segment")
        critique = "These codes just paraphrase the segment. Sharpen them."
        refined = _single("analytic-concept", "segment text we feed")

        llm, calls = _fake_llm([first, critique, refined])
        coder = _coder_with_llm(llm)
        agent = RefiningCoderAgent(coder)

        result = agent.code_segment(_seg(SEG_TEXT))

        assert [c.code for c in result] == ["analytic-concept"]
        assert len(calls) == 3

        coder1, critic_call, coder2 = calls

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

        assert [m.role for m in critic_call.messages] == ["system", "user"]
        assert critic_call.messages[0].content[0].text == CRITIC_SYSTEM_PROMPT
        assert critic_call.response_format is None

        refine_user_text = coder2.messages[3].content[0].text
        assert critique in refine_user_text
        assert "independent reviewer" in refine_user_text.lower()

        assert coder1.response_format is not None
        assert coder2.response_format is not None
        assert coder1.response_format["type"] == "json_schema"
        assert coder2.response_format["type"] == "json_schema"

    def test_empty_first_pass_skips_critic_and_refinement(self):
        llm, calls = _fake_llm([_json_codes([])])
        coder = _coder_with_llm(llm)
        agent = RefiningCoderAgent(coder)

        result = agent.code_segment(_seg("off-topic chatter"))

        assert len(result) == 1 and is_sentinel_code(result[0])
        assert len(calls) == 1

    def test_refined_response_replaces_first(self):
        llm, calls = _fake_llm(
            [
                _json_codes([
                    ("A", "d", ["actual segment"]),
                    ("B", "d", ["segment text"]),
                    ("C", "d", ["the actual"]),
                    ("D", "d", ["we feed"]),
                ]),
                "merge A and B; C is fine; D is a paraphrase",
                _json_codes([
                    ("merged-AB", "d", ["actual segment"]),
                    ("C", "d", ["the actual"]),
                ]),
            ]
        )
        agent = RefiningCoderAgent(_coder_with_llm(llm))
        result = agent.code_segment(_seg(SEG_TEXT))
        assert [c.code for c in result] == ["merged-AB", "C"]
        assert len(calls) == 3

    def test_critic_chat_excludes_codebook_and_identity(self):
        existing = SimpleNamespace(code="existing-code")
        rc = _rc_row(description="Study of gendered narratives.")
        coder = CoderAgent(
            coder=_coder_stub(identity="feminist scholar"),
            codebook=_codebook_stub(codes=[existing], research_context=rc),
        )
        llm, calls = _fake_llm(
            [
                _single("x", "actual segment"),
                "weak code",
                _single("x-refined", "segment text"),
            ]
        )
        coder._llm = llm

        agent = RefiningCoderAgent(coder)
        agent.code_segment(_seg(SEG_TEXT))

        critic_sys = calls[1].messages[0].content[0].text
        critic_user = calls[1].messages[1].content[0].text
        full_critic_chat = critic_sys + "\n" + critic_user

        assert "existing-code" not in full_critic_chat
        assert "feminist scholar" not in full_critic_chat

        assert SEG_TEXT in critic_user
        assert "x" in critic_user

        assert "gendered narratives" in critic_sys

    def test_critic_inherits_research_context_from_codebook(self):
        rc = _rc_row(description="Late context attached to codebook.")
        coder = _coder_with_llm(
            _fake_llm(
                [_single("c", "actual segment"), "critique",
                 _single("c'", "segment text")]
            )[0],
            codebook=_codebook_stub(research_context=rc),
        )
        agent = RefiningCoderAgent(coder)
        assert agent.critic.research_context is not None
        assert "Late context" in agent.critic.research_context.description

    @pytest.mark.asyncio
    async def test_async_flow_mirrors_sync(self):
        llm, calls = _fake_llm(
            [
                _single("shallow", "actual segment"),
                "critique",
                _single("deeper", "segment text"),
            ]
        )
        agent = RefiningCoderAgent(_coder_with_llm(llm))
        result = await agent.code_segment_async(_seg(SEG_TEXT))

        assert [c.code for c in result] == ["deeper"]
        assert len(calls) == 3
        assert [m.role for m in calls[2].messages] == [
            "system",
            "user",
            "assistant",
            "user",
        ]

    def test_custom_critic_is_used(self):
        coder_llm, coder_calls = _fake_llm(
            [_single("c", "actual segment"), _single("c'", "segment text")]
        )
        critic_llm, critic_calls = _fake_llm(["explicit critic spoke"])
        coder = _coder_with_llm(coder_llm)
        agent = RefiningCoderAgent(coder, critic=Critic(critic_llm))
        agent.code_segment(_seg(SEG_TEXT))

        assert len(coder_calls) == 2
        assert len(critic_calls) == 1
        refine_text = coder_calls[1].messages[3].content[0].text
        assert "explicit critic spoke" in refine_text


class TestWrapWithRefinement:
    def test_wraps_coder_agent(self):
        coder = CoderAgent(coder=_coder_stub(), codebook=_codebook_stub())
        wrapped = wrap_with_refinement(coder)
        assert isinstance(wrapped, RefiningCoderAgent)
        assert wrapped.coder is coder

    def test_passes_through_non_coder(self):
        sentinel = object()
        assert wrap_with_refinement(sentinel) is sentinel


class TestWorkerDefaultFactory:
    def test_default_factory_returns_refining_agent(self):
        codebook = _codebook_stub()
        coder_row = store.Coder(coder_id=1, identity="x")
        agent = workers.default_coder_factory(codebook, coder_row)
        assert isinstance(agent, RefiningCoderAgent)
        assert isinstance(agent.coder, CoderAgent)
        assert agent.coder.coder.identity == "x"


class TestWorkerEndToEnd:
    """workers.code_one drives RefiningCoderAgent and persists the
    *refined* codes, not the first pass."""

    def test_code_one_persists_refined_codes(self, tmp_path: Path):
        db = tmp_path / "x.sqlite"
        conn = store.init_db(db)
        c = store.add_coder("id1")
        doc = store.add_document("d.md")
        # Use a segment whose substring "text 0" appears in any quote
        # we craft below.
        store.enqueue_segments(doc, [(None, "text 0", 0, 0, 0)])
        store.coding.enqueue_document(doc.document_id)

        llm, _ = _fake_llm(
            [
                _single("shallow", "text 0"),
                "the codes are too shallow",
                _single("deeper-concept", "text 0"),
            ]
        )

        def factory(codebook, coder):
            base = CoderAgent(coder=coder, codebook=codebook)
            base._llm = llm
            return wrap_with_refinement(base)

        res = workers.code_one(
            c.coder_id, use_mock_embeddings=True, agent_factory=factory
        )
        assert res is not None and res["ok"], res
        assert res["n_codes"] == 1

        codes = conn.execute(
            "SELECT code, description FROM code WHERE coder_id = ? ORDER BY code_id",
            (c.coder_id,),
        ).fetchall()
        assert [row["code"] for row in codes] == ["deeper-concept"]
        assert codes[0]["description"] == "desc of deeper-concept"

        # Quote row is persisted and linked.
        rows = conn.execute(
            "SELECT q.text FROM quote q "
            "JOIN codes_supporting_quotes csq ON csq.quote_id = q.quote_id "
            "JOIN code c ON c.code_id = csq.code_id "
            "WHERE c.coder_id = ?",
            (c.coder_id,),
        ).fetchall()
        assert [row["text"] for row in rows] == ["text 0"]
