"""Adversarial refinement step for the incremental Stage 1 coder.

Three LLM calls per segment, in two separate chat sessions:

1. **Coder chat (call 1).** The configured ``CoderAgent`` runs with its
   normal prompt — codebook, identity, research context, the lot — and
   returns an initial set of codes.

2. **Critic chat (call 2).** A fresh chat session with a minimal,
   adversarial system prompt. The critic sees *only* the original
   segment and the codes the coder just produced; it does **not** see
   the codebook, the coder's identity, similar-codes hints, or the
   research context. Stripping that scaffolding lets the critic push
   back hard without being anchored to the coder's framing.

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
from typing import TYPE_CHECKING, Any

from openhands.sdk import LLM, Message, TextContent

from thematic_analysis.agents.coder import (
    CODER_RESPONSE_SCHEMA,
    CodeAssignment,
    CoderAgent,
)


if TYPE_CHECKING:
    from thematic_analysis.research_context import ResearchContext


CRITIC_SYSTEM_PROMPT = """\
You are a tough, sceptical reviewer of qualitative coding. Another
researcher has produced codes for a single text segment, and your job
is to push back on their work, not to compliment it. Assume the codes
are weaker than they should be unless the segment plainly justifies
them.

Attack the codes specifically on:

1. **Shallow paraphrase.** Are the codes just restating what the
   segment literally says, rather than naming an analytic concept
   that could re-occur across other texts?
2. **Over-reach.** Did the coder generalise beyond what the segment
   actually supports? Quote the text (or note its absence) when you
   make this point.
3. **Missed content.** Is there meaningful content in the segment
   that the codes ignore?
4. **Conflation or overlap.** Do two of the codes name the same
   concept? Is one code lumping together two distinct ideas that
   should be split?
5. **Vagueness.** Are the labels so generic they would apply to
   almost any text?

Be specific and quote the segment when you object. If, after looking
hard for problems, you genuinely think the codes are good, say so
plainly and explain why — but only after you have looked.

Output a short critique in plain prose addressed to the coder. No
JSON, no headers, no checklist — just the critique."""


CRITIC_RESEARCH_CONTEXT_GUIDANCE = """\
Use the research context above to judge whether the codes are actually
relevant to the research focus. A code that fairly describes the segment
but speaks to something outside the research focus should be challenged —
codes must be responsive to the research questions, not just locally
accurate."""


def _build_critic_user_prompt(
    segment_text: str, codes: list[str], rationales: list[str]
) -> str:
    if rationales and len(rationales) == len(codes):
        coded = "\n".join(
            f"{i + 1}. {code}  —  {rationale}"
            for i, (code, rationale) in enumerate(zip(codes, rationales))
        )
    else:
        coded = "\n".join(f"{i + 1}. {code}" for i, code in enumerate(codes))
    return (
        "## Text Segment\n"
        f'"""\n{segment_text}\n"""\n\n'
        "## Codes the researcher produced\n"
        f"{coded}\n\n"
        "Now critique these codes."
    )


def _build_refinement_user_prompt(critique: str) -> str:
    return (
        "An independent reviewer was given only this segment and the codes "
        "you produced — no codebook, no perspective notes, no other "
        "context — and raised the following critique:\n"
        "\n"
        "-----\n"
        f"{critique.strip()}\n"
        "-----\n"
        "\n"
        "Re-examine your codes in light of this critique. Where the "
        "reviewer is right, revise: rename, drop, split, merge, or add "
        "codes as appropriate. Where the reviewer is wrong, keep your "
        "original choice but make sure you have a defensible reason. "
        "Return the final code set as JSON using the same schema "
        "(`codes`, `rationales`, `is_new`). No commentary outside the "
        "JSON object."
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
        if ctx is None or ctx.is_empty():
            return CRITIC_SYSTEM_PROMPT
        return (
            CRITIC_SYSTEM_PROMPT
            + "\n\n## Research Context\n"
            + ctx.to_prompt_section()
            + "\n\n"
            + CRITIC_RESEARCH_CONTEXT_GUIDANCE
        )

    def _messages(
        self, segment_text: str, codes: list[str], rationales: list[str]
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
                        text=_build_critic_user_prompt(
                            segment_text, codes, rationales
                        )
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

    def critique(
        self, segment_text: str, codes: list[str], rationales: list[str]
    ) -> str:
        response = self.llm.completion(
            messages=self._messages(segment_text, codes, rationales)
        )
        return self._extract_text(response)

    async def critique_async(
        self, segment_text: str, codes: list[str], rationales: list[str]
    ) -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.llm.completion(
                messages=self._messages(segment_text, codes, rationales)
            ),
        )
        return self._extract_text(response)


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

    def _initial_messages(self, segment_id: str, text: str) -> list[Message]:
        system_prompt = self.coder.get_system_prompt()
        user_prompt = self.coder._build_user_prompt(segment_id, text)
        return [
            Message(role="system", content=[TextContent(text=system_prompt)]),
            Message(role="user", content=[TextContent(text=user_prompt)]),
        ]

    @staticmethod
    def _append_turn(
        messages: list[Message], role: str, text: str
    ) -> list[Message]:
        return messages + [
            Message(role=role, content=[TextContent(text=text)]),
        ]

    def _coder_completion(self, messages: list[Message]) -> str:
        response = self.coder.llm.completion(
            messages=messages, response_format=CODER_RESPONSE_SCHEMA
        )
        return self.coder._extract_text(response)

    async def _coder_completion_async(self, messages: list[Message]) -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.coder.llm.completion(
                messages=messages, response_format=CODER_RESPONSE_SCHEMA
            ),
        )
        return self.coder._extract_text(response)

    # ── public API matching CoderAgent ───────────────────────────────────

    def code_segment(self, segment_id: str, text: str) -> CodeAssignment:
        initial_msgs = self._initial_messages(segment_id, text)
        first_response = self._coder_completion(initial_msgs)
        first_assignment = self.coder._process_response(
            first_response, segment_id, text
        )
        if not first_assignment.codes:
            return first_assignment

        critique = self.critic.critique(
            text, first_assignment.codes, first_assignment.rationales
        )

        refined_msgs = self._append_turn(
            initial_msgs, "assistant", first_response
        )
        refined_msgs = self._append_turn(
            refined_msgs, "user", _build_refinement_user_prompt(critique)
        )
        refined_response = self._coder_completion(refined_msgs)
        return self.coder._process_response(refined_response, segment_id, text)

    async def code_segment_async(
        self, segment_id: str, text: str
    ) -> CodeAssignment:
        initial_msgs = self._initial_messages(segment_id, text)
        first_response = await self._coder_completion_async(initial_msgs)
        first_assignment = self.coder._process_response(
            first_response, segment_id, text
        )
        if not first_assignment.codes:
            return first_assignment

        critique = await self.critic.critique_async(
            text, first_assignment.codes, first_assignment.rationales
        )

        refined_msgs = self._append_turn(
            initial_msgs, "assistant", first_response
        )
        refined_msgs = self._append_turn(
            refined_msgs, "user", _build_refinement_user_prompt(critique)
        )
        refined_response = await self._coder_completion_async(refined_msgs)
        return self.coder._process_response(refined_response, segment_id, text)


def wrap_with_refinement(agent: Any) -> Any:
    """Wrap a ``CoderAgent`` so it does a critic-driven refinement pass.

    Non-``CoderAgent`` inputs are returned unchanged so callers passing
    in custom or stubbed agents are unaffected.
    """
    if isinstance(agent, CoderAgent):
        return RefiningCoderAgent(agent)
    return agent
