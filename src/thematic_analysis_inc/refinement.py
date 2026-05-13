"""Refinement step for the incremental Stage 1 coder.

The first coding pass often produces shallow, descriptive labels that read
more like summaries than analytic codes grounded in the research question.
``RefiningCoderAgent`` wraps a ``CoderAgent`` and follows up the initial
response with a second turn in the *same* chat session that challenges the
codes and asks the model to refine them. Keeping the conversation in one
session means the prefix (system prompt + initial user prompt + initial
assistant reply) is identical across the two calls, which is what
Anthropic prompt caching keys on.

If the first pass returns an empty assignment (e.g. OUT_OF_SCOPE) the
refinement step is skipped — there are no codes to challenge.
"""

from __future__ import annotations

import asyncio
from typing import Any

from openhands.sdk import Message, TextContent

from thematic_analysis.agents.coder import (
    CODER_RESPONSE_SCHEMA,
    CodeAssignment,
    CoderAgent,
)


REFINEMENT_USER_PROMPT = """\
Now critically review the codes you just produced.

First-pass codes are often shallow: they restate what the segment says
instead of capturing the analytic concept it speaks to. Challenge your
own output against the following criteria:

1. **Depth over summary.** Does each code name an analytic concept that
   could re-appear in other segments, or is it just a paraphrase of this
   one passage? If it's a paraphrase, sharpen it into a concept.
2. **Relevance to the research focus.** Does each code speak to the
   research questions / theoretical framework, or did it drift toward
   descriptive surface features? Drop codes that don't connect; tighten
   codes that connect weakly.
3. **Grounding.** Is each code supported by something specific in the
   text, or were you generalising beyond what the segment says? Remove
   codes that aren't grounded.
4. **Granularity.** Are two codes really the same concept? Merge them.
   Is one code lumping together two distinct concepts? Split it.
5. **Missed content.** Is there content in the segment that addresses
   the research focus but went uncoded? Add codes for it.

If, after this review, your original codes are the best ones, return
them unchanged. Otherwise return the refined set. Use the same JSON
schema as before (`codes`, `rationales`, `is_new`). Do not include any
commentary outside the JSON object."""


class RefiningCoderAgent:
    """Two-turn coder: code, then challenge-and-refine in the same session.

    Delegates prompt construction and parsing to an underlying
    ``CoderAgent`` so it stays in lock-step with the rest of the pipeline
    (research context, identity, codebook formatting, response schema).
    The two LLM calls reuse the same message prefix to allow prompt
    caching on the underlying provider.
    """

    def __init__(self, coder: CoderAgent):
        self.coder = coder

    # The worker honours these attributes via duck-typing.
    @property
    def research_context(self):  # pragma: no cover - trivial delegation
        return self.coder.research_context

    @research_context.setter
    def research_context(self, value) -> None:  # pragma: no cover - trivial
        self.coder.research_context = value

    @property
    def codebook(self):  # pragma: no cover - trivial delegation
        return self.coder.codebook

    # ── internal helpers ─────────────────────────────────────────────────

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

    def _completion(self, messages: list[Message]) -> str:
        response = self.coder.llm.completion(
            messages=messages, response_format=CODER_RESPONSE_SCHEMA
        )
        return self.coder._extract_text(response)

    async def _completion_async(self, messages: list[Message]) -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.coder.llm.completion(
                messages=messages, response_format=CODER_RESPONSE_SCHEMA
            ),
        )
        return self.coder._extract_text(response)

    def _is_empty(self, assignment: CodeAssignment) -> bool:
        return not assignment.codes

    def _refine(
        self,
        first_response: str,
        first_assignment: CodeAssignment,
        initial_messages: list[Message],
        segment_id: str,
        text: str,
    ) -> CodeAssignment:
        if self._is_empty(first_assignment):
            return first_assignment
        messages = self._append_turn(initial_messages, "assistant", first_response)
        messages = self._append_turn(messages, "user", REFINEMENT_USER_PROMPT)
        refined_response = self._completion(messages)
        refined = self.coder._process_response(refined_response, segment_id, text)
        if self._is_empty(refined):
            # Refinement decided everything was off-topic. Trust it.
            return refined
        return refined

    async def _refine_async(
        self,
        first_response: str,
        first_assignment: CodeAssignment,
        initial_messages: list[Message],
        segment_id: str,
        text: str,
    ) -> CodeAssignment:
        if self._is_empty(first_assignment):
            return first_assignment
        messages = self._append_turn(initial_messages, "assistant", first_response)
        messages = self._append_turn(messages, "user", REFINEMENT_USER_PROMPT)
        refined_response = await self._completion_async(messages)
        return self.coder._process_response(refined_response, segment_id, text)

    # ── public API matching CoderAgent ───────────────────────────────────

    def code_segment(self, segment_id: str, text: str) -> CodeAssignment:
        messages = self._initial_messages(segment_id, text)
        first_response = self._completion(messages)
        first_assignment = self.coder._process_response(
            first_response, segment_id, text
        )
        return self._refine(
            first_response, first_assignment, messages, segment_id, text
        )

    async def code_segment_async(
        self, segment_id: str, text: str
    ) -> CodeAssignment:
        messages = self._initial_messages(segment_id, text)
        first_response = await self._completion_async(messages)
        first_assignment = self.coder._process_response(
            first_response, segment_id, text
        )
        return await self._refine_async(
            first_response, first_assignment, messages, segment_id, text
        )


def wrap_with_refinement(agent: Any) -> Any:
    """Wrap a ``CoderAgent`` so it does a refinement pass after coding.

    Non-``CoderAgent`` inputs are returned unchanged so callers passing in
    custom or stubbed agents are unaffected.
    """
    if isinstance(agent, CoderAgent):
        return RefiningCoderAgent(agent)
    return agent
