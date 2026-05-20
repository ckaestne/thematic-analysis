"""Adversarial refinement step for the incremental Stage 1 coder.

Three LLM calls per segment, in two separate chat sessions:

1. **Coder chat (call 1).** The configured ``CoderAgent`` runs with its
   normal prompt — codebook, identity, research context, the lot — and
   returns an initial set of codes.

2. **Critic chat (call 2).** A fresh chat session with a minimal,
   adversarial system prompt. The critic sees the segment, the codes
   the coder just produced, and the research context (so it can judge
   relevance). It does **not** see the codebook, the coder's identity,
   or similar-codes hints, so it can push back hard without being
   anchored to the coder's framing.

3. **Coder chat (call 3).** We return to the coder's original session
   and append the critique as a user turn, asking the coder to revise.
   Because the prefix ``[system, user1, assistant1]`` is identical to
   call 1 plus the assistant reply, the provider can reuse the cached
   prefix for this turn.

If the first pass returns no codes (e.g. OUT_OF_SCOPE), the critic and
refinement turns are skipped — there is nothing to critique.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from openhands.sdk import LLM, Message, TextContent

from thematic_analysis.agents.base import (
    _RETRY_MAX_ATTEMPTS,
    _backoff_delay,
    _is_retryable,
)
from thematic_analysis.agents.coder import (
    CODER_RESPONSE_SCHEMA,
    CoderAgent,
)
from thematic_analysis.prompts import join_system_prompt_sections
from thematic_analysis_inc.db.models import Code, Segment, is_sentinel_code


if TYPE_CHECKING:
    from thematic_analysis.research_context import ResearchContext


CRITIC_SYSTEM_PROMPT = """\
You are a sceptical reviewer of qualitative coding. Another researcher
has produced codes for a single text segment. Your job is to challenge
those codes against the research focus — not to ratify them, and not
to invent justifications for keeping them.

Your priorities, in order:

1. **Relevance to the research focus.** Codes must be directly
   relevant to the research question(s), not merely describe what the
   segment is about. Many segments are simply off-topic for this
   study and should not be coded at all. If this
   segment (or parts of it) does not address the research focus, say
   so plainly and recommend dropping the off-topic codes. Recommending
   that the coder drop *all* codes — leaving the segment uncoded — is
   a correct and expected outcome when nothing in the segment speaks
   to the research question.
2. **Themes over summary.** Among codes that are on-topic, are they
   naming analytic features that could recur across segments, or just
   paraphrasing what this segment says? Push for themes.
3. **Grounding.** Did the coder generalise beyond what the segment
   supports, or miss on-topic content?

Be specific and quote the segment when you object. End with concrete
recommendations: which codes to drop as off-topic, which to rename or
sharpen, and (if appropriate) what on-topic content was missed. Do not
soften the critique to be polite; if the codes are off-topic, say so.

Output a short critique in plain prose. No JSON, no headers."""


def _format_codes_for_critic(codes: list[Code]) -> str:
    blocks: list[str] = []
    for i, c in enumerate(codes, 1):
        header = f"{i}. {c.code} — {c.description}" if c.description else f"{i}. {c.code}"
        quote_lines = [
            f'   - "{q.text}"' for q in (c.supporting_quotes or [])
        ]
        blocks.append("\n".join([header, *quote_lines]) if quote_lines else header)
    return "\n".join(blocks)


def _build_critic_user_prompt(segment_text: str, codes: list[Code]) -> str:
    return (
        "## Text Segment\n"
        f'"""\n{segment_text}\n"""\n\n'
        "## Codes the researcher produced\n"
        f"{_format_codes_for_critic(codes)}\n\n"
        "Now critique these codes."
    )


def _build_refinement_user_prompt(critique: str) -> str:
    return (
        "An independent reviewer was given only this segment, your codes, "
        "and the research context — no codebook, no perspective notes — "
        "and raised the following critique:\n"
        "\n"
        "-----\n"
        f"{critique.strip()}\n"
        "-----\n"
        "\n"
        "Use this critique to produce a stronger set of codes. Drop, "
        "rename, split, merge, or add codes as needed. If the reviewer "
        "argues that the segment (or part of it) is not relevant to the "
        "research focus, drop those codes — an empty `codes` list is a "
        "valid answer when nothing in the segment speaks to the research "
        "question. Return the final code set as JSON using the same "
        "schema. No commentary outside the JSON object."
    )


class Critic:
    """Adversarial reviewer in a fresh chat session.

    Sees the segment text, the codes (with rationales), and — if set —
    the research context, which it needs in order to judge relevance.
    Deliberately does **not** see the codebook, the coder's identity,
    or similar-codes hints, so it can't be anchored by the coder's
    framing.
    """

    def __init__(
        self,
        llm: LLM,
        research_context: "ResearchContext | None" = None,
    ):
        self.llm = llm
        self.research_context = research_context

    def _system_prompt(self) -> str:
        ctx = self.research_context
        research_section = ""
        if ctx is not None and not ctx.is_empty():
            research_section = ctx.to_prompt_section(role="coding_critic")
        return join_system_prompt_sections(
            CRITIC_SYSTEM_PROMPT,
            research_context_instructions=research_section,
        )

    def _messages(
        self, segment_text: str, codes: list[Code]
    ) -> list[Message]:
        return [
            Message(
                role="system",
                content=[TextContent(text=self._system_prompt())],
            ),
            Message(
                role="user",
                content=[
                    TextContent(
                        text=_build_critic_user_prompt(segment_text, codes)
                    )
                ],
            ),
        ]

    @staticmethod
    def _extract_text(response: Any) -> str:
        parts: list[str] = []
        for part in response.message.content:
            if isinstance(part, TextContent):
                parts.append(part.text)
        return "".join(parts)

    def critique(self, segment_text: str, codes: list[Code]) -> str:
        msgs = self._messages(segment_text, codes)
        last: BaseException | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                response = self.llm.completion(messages=msgs)
                return self._extract_text(response)
            except Exception as exc:
                if not _is_retryable(exc) or attempt == _RETRY_MAX_ATTEMPTS - 1:
                    raise
                last = exc
                time.sleep(_backoff_delay(attempt, exc))
        assert last is not None
        raise last

    async def critique_async(
        self, segment_text: str, codes: list[Code]
    ) -> str:
        msgs = self._messages(segment_text, codes)
        loop = asyncio.get_event_loop()
        last: BaseException | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                response = await loop.run_in_executor(
                    None, lambda: self.llm.completion(messages=msgs)
                )
                return self._extract_text(response)
            except Exception as exc:
                if not _is_retryable(exc) or attempt == _RETRY_MAX_ATTEMPTS - 1:
                    raise
                last = exc
                await asyncio.sleep(_backoff_delay(attempt, exc))
        assert last is not None
        raise last


class RefiningCoderAgent:
    """Two-chat refinement: code → adversarial critique → refine.

    Delegates the coding prompt and parsing to an underlying
    ``CoderAgent`` so the refinement flow stays in lock-step with the
    rest of the pipeline (research context, identity, codebook
    formatting, response schema). The coder chat reuses one message
    prefix for calls 1 and 3 to allow provider-side prompt caching; the
    critic chat is a separate session with a minimal prompt.
    """

    def __init__(self, coder: CoderAgent, critic: Critic | None = None):
        self.coder = coder
        self._critic = critic
        self.last_trace: dict | None = None

    @property
    def critic(self) -> Critic:
        """The adversarial critic. Created lazily so wrapping a coder
        whose LLM isn't configured yet doesn't trigger env lookup.
        Inherits the coder's current research context at creation."""
        if self._critic is None:
            self._critic = Critic(
                self.coder.llm,
                research_context=self.coder.research_context,
            )
        return self._critic

    # Duck-typed attributes the worker layer reads/writes.
    @property
    def research_context(self):
        return self.coder.research_context

    @research_context.setter
    def research_context(self, value) -> None:
        """Propagate research context to both the coder and the critic.

        The worker layer sets this on the wrapper before each coding
        call, so the critic needs to pick it up too — without the
        research context the critic can't judge whether codes are
        responsive to the research question.
        """
        self.coder.research_context = value
        if self._critic is not None:
            self._critic.research_context = value

    @property
    def codebook(self):  # pragma: no cover - trivial delegation
        return self.coder.codebook

    # ── coder-chat helpers ───────────────────────────────────────────────

    def _initial_messages(
        self, segment: Segment
    ) -> tuple[str, str, list[Message]]:
        system_prompt = self.coder.get_system_prompt()
        user_prompt = self.coder._build_user_prompt(segment)
        messages = [
            Message(role="system", content=[TextContent(text=system_prompt)]),
            Message(role="user", content=[TextContent(text=user_prompt)]),
        ]
        return system_prompt, user_prompt, messages

    @staticmethod
    def _append_turn(
        messages: list[Message], role: str, text: str
    ) -> list[Message]:
        return messages + [
            Message(role=role, content=[TextContent(text=text)]),
        ]

    def _coder_completion(self, messages: list[Message]) -> str:
        response = self.coder._completion_with_retry(
            messages, {"response_format": CODER_RESPONSE_SCHEMA}
        )
        return self.coder._extract_text(response)

    async def _coder_completion_async(self, messages: list[Message]) -> str:
        response = await self.coder._completion_with_retry_async(
            messages, {"response_format": CODER_RESPONSE_SCHEMA}
        )
        return self.coder._extract_text(response)

    # ── public API matching CoderAgent ───────────────────────────────────

    def code_segment(self, segment: Segment) -> list[Code]:
        self.last_trace = None
        text = segment.content
        coder_system_prompt, coder_user_prompt, initial_msgs = (
            self._initial_messages(segment)
        )
        first_response = self._coder_completion(initial_msgs)
        first_codes = self.coder._parse_response(first_response, segment)
        if all(is_sentinel_code(c) for c in first_codes):
            self.last_trace = {
                "segment_text": text,
                "first": first_codes,
                "critique": None,
                "refined": None,
                "coder_system_prompt": coder_system_prompt,
                "coder_user_prompt": coder_user_prompt,
                "critic_system_prompt": None,
                "critic_user_prompt": None,
                "refinement_user_prompt": None,
            }
            return first_codes

        critic_system_prompt = self.critic._system_prompt()
        critic_user_prompt = _build_critic_user_prompt(text, first_codes)
        critique = self.critic.critique(text, first_codes)

        refinement_user_prompt = _build_refinement_user_prompt(critique)
        refined_msgs = self._append_turn(
            initial_msgs, "assistant", first_response
        )
        refined_msgs = self._append_turn(
            refined_msgs, "user", refinement_user_prompt
        )
        refined_response = self._coder_completion(refined_msgs)
        refined_codes = self.coder._parse_response(refined_response, segment)
        self.last_trace = {
            "segment_text": text,
            "first": first_codes,
            "critique": critique,
            "refined": refined_codes,
            "coder_system_prompt": coder_system_prompt,
            "coder_user_prompt": coder_user_prompt,
            "critic_system_prompt": critic_system_prompt,
            "critic_user_prompt": critic_user_prompt,
            "refinement_user_prompt": refinement_user_prompt,
        }
        return refined_codes

    async def code_segment_async(self, segment: Segment) -> list[Code]:
        self.last_trace = None
        text = segment.content
        coder_system_prompt, coder_user_prompt, initial_msgs = (
            self._initial_messages(segment)
        )
        first_response = await self._coder_completion_async(initial_msgs)
        first_codes = self.coder._parse_response(first_response, segment)
        if all(is_sentinel_code(c) for c in first_codes):
            self.last_trace = {
                "segment_text": text,
                "first": first_codes,
                "critique": None,
                "refined": None,
                "coder_system_prompt": coder_system_prompt,
                "coder_user_prompt": coder_user_prompt,
                "critic_system_prompt": None,
                "critic_user_prompt": None,
                "refinement_user_prompt": None,
            }
            return first_codes

        critic_system_prompt = self.critic._system_prompt()
        critic_user_prompt = _build_critic_user_prompt(text, first_codes)
        critique = await self.critic.critique_async(text, first_codes)

        refinement_user_prompt = _build_refinement_user_prompt(critique)
        refined_msgs = self._append_turn(
            initial_msgs, "assistant", first_response
        )
        refined_msgs = self._append_turn(
            refined_msgs, "user", refinement_user_prompt
        )
        refined_response = await self._coder_completion_async(refined_msgs)
        refined_codes = self.coder._parse_response(refined_response, segment)
        self.last_trace = {
            "segment_text": text,
            "first": first_codes,
            "critique": critique,
            "refined": refined_codes,
            "coder_system_prompt": coder_system_prompt,
            "coder_user_prompt": coder_user_prompt,
            "critic_system_prompt": critic_system_prompt,
            "critic_user_prompt": critic_user_prompt,
            "refinement_user_prompt": refinement_user_prompt,
        }
        return refined_codes


def wrap_with_refinement(agent: Any) -> Any:
    """Wrap a ``CoderAgent`` so it does a critic-driven refinement pass.

    Non-``CoderAgent`` inputs are returned unchanged so callers passing
    in custom or stubbed agents are unaffected.
    """
    if isinstance(agent, CoderAgent):
        return RefiningCoderAgent(agent)
    return agent
