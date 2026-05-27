"""Coder agent for assigning codes to text segments.

Following Thematic-LM (Sec. 3 and Appendix "Main Prompts"), the coder
writes 1–3 codes per segment. For each code, it provides a short
description of the analytic concept and extracts one or more verbatim
quotes from the segment as evidence.

The agent is bound to one ``Coder`` and one ``Codebook`` revision via
its constructor. Each returned ``Code`` carries ``segment_id``,
``coder_id``, and ``codebook_used_id`` already set, so the worker can
``s.add(c)`` without rebuilding the row. Research context is read from
``codebook.research_context``; it is not a separate constructor arg.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

from openhands.sdk import Message, TextContent
from pydantic import BaseModel, ValidationError

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_json_str
from thematic_analysis.prompts import (
    CODER_SYSTEM_PROMPT,
    CODER_USER_PROMPT,
    join_system_prompt_sections,
)
from thematic_analysis_inc.db.models import (
    SENTINEL_CODE_LABEL,
    Code,
    Codebook,
    Coder,
    Quote,
    Segment,
    create_quote,
)
from thematic_analysis_inc.db.research_context import (
    to_domain as _research_context_to_domain,
)


if TYPE_CHECKING:
    from thematic_analysis.prompts import CoderPrompts


@dataclass
class CoderConfig(AgentConfig):
    """Configuration for the Coder agent."""

    task: str = "coder"
    max_codes_per_segment: int = 5
    include_6rs_guidance: bool = True
    custom_prompts: CoderPrompts | None = None


class _CodeItem(BaseModel):
    """Pydantic shape mirroring the JSON-schema item the LLM returns."""

    code: str
    description: str
    quotes: list[str]

    model_config = {"extra": "ignore"}


class _CoderResponse(BaseModel):
    codes: list[_CodeItem]

    model_config = {"extra": "ignore"}


class QuoteVerificationError(Exception):
    """Raised when the coder returns quotes that don't appear in the segment.

    Carries the list of `(code, [rejected_quote, ...])` so callers can
    surface a follow-up message asking the LLM to revise.
    """

    def __init__(self, bad_codes: list[tuple[str, list[str]]]):
        self.bad_codes = bad_codes
        detail = "; ".join(
            f"{code!r}: {quotes!r}" for code, quotes in bad_codes
        )
        super().__init__(
            f"Coder returned quotes not verbatim in segment: {detail}"
        )


# Characters stripped before the substring check. Covers:
#   - C0 control chars except the whitespace ones (\t \n \v \f \r), e.g.
#     \x12 from PDFs in place of an "fi" ligature ("arti\x12cial").
#   - DEL (\x7f).
#   - Soft hyphen (U+00AD) — common in PDF text extraction.
#   - Zero-width chars (U+200B-U+200D, U+FEFF).
#   - U+FFFD replacement character — PDFs render lost ligatures as
#     this, and LLMs typically drop it when echoing quotes.
_NON_WS_CTRL_RE = re.compile(
    "["
    "\x00-\x08\x0e-\x1f\x7f"      # C0 controls except \t \n \v \f \r; DEL
    "­"                       # soft hyphen
    "​-‏"                # zero-width space / joiner / non-joiner / RLM/LRM
    "⁠-⁯"                # word joiner & invisible operators
    "﻿"                       # BOM / zero-width no-break space
    "﷐-﷯"                # noncharacters in BMP
    "￰-￿"                # specials (incl. U+FFFD/U+FFFE/U+FFFF)
    "]"
)


def _normalize_for_match(s: str) -> str:
    """Loose normalization for the verbatim-quote check.

    Strips non-whitespace C0 control characters (PDF ligature artifacts),
    collapses runs of whitespace — including newlines and tabs — to a
    single space, and casefolds. Used only for the substring check; the
    quote text stored on the Quote row is still the LLM's original
    string.
    """
    s = _NON_WS_CTRL_RE.sub("", s)
    s = " ".join(s.split())
    return s.casefold()


def _build_quote_retry_user_prompt(
    bad_codes: list[tuple[str, list[str]]],
) -> str:
    blocks: list[str] = []
    for code, quotes in bad_codes:
        qlines = "\n".join(f"  - {q!r}" for q in quotes)
        blocks.append(f"- Code {code!r}, rejected quotes:\n{qlines}")
    detail = "\n".join(blocks)
    return (
        "Some of the quotes in your previous response are not verbatim "
        "substrings of the segment text and were rejected:\n"
        f"{detail}\n\n"
        "Please revise your response. Every quote must be a character-for-"
        "character substring of the segment as it appears above — do not "
        "paraphrase, splice across sentences, or modify punctuation or "
        "whitespace. Return the full corrected JSON object using the same "
        "schema. If you cannot find a verbatim supporting quote for a code, "
        "drop that code."
    )


CODER_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_assignment",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "codes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "code": {"type": "string"},
                            "description": {"type": "string"},
                            "quotes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["code", "description", "quotes"],
                    },
                },
            },
            "required": ["codes"],
        },
    },
}


class CoderAgent(BaseAgent):
    """Agent that assigns codes to text segments.

    The ``Coder`` and ``Codebook`` come in via the constructor — the
    agent stamps ``coder_id`` and ``codebook_used_id`` from them onto
    every returned ``Code`` (real or sentinel). The codebook also
    carries the research context via ``codebook.research_context``.
    """

    def __init__(
        self,
        coder: Coder,
        codebook: Codebook,
        config: CoderConfig | None = None,
    ):
        super().__init__(config or CoderConfig())
        self.coder_config: CoderConfig = self.config  # type: ignore
        self.coder = coder
        self.codebook = codebook
        # Populated by parse_with_retry[_async] when a verbatim-quote
        # retry happens; consumers (RefiningCoderAgent, CLI) read it to
        # surface intermediate results. ``None`` when no retry occurred.
        self.last_quote_retry: dict | None = None

    def get_system_prompt(self) -> str:
        identity_section = ""
        if self.coder.identity:
            identity_section = (
                "\n## Your Perspective:\n"
                "You are coding from the following perspective: "
                f"{self.coder.identity}\n"
                "Let this perspective inform how you interpret and code the "
                "data, while maintaining\n"
                "analytical rigor and staying grounded in the text."
            )

        research_section = ""
        rc_row = self.codebook.research_context
        if rc_row is not None:
            rc = _research_context_to_domain(rc_row)
            if not rc.is_empty():
                research_section = (
                    "## Research context\n" + rc.to_prompt_section(role="coder")
                )

        base_prompt = (
            self.coder_config.custom_prompts.system_prompt
            if self.coder_config.custom_prompts is not None
            else CODER_SYSTEM_PROMPT
        )
        return join_system_prompt_sections(
            base_prompt,
            research_context_instructions=research_section,
            identity_instructions=identity_section,
        )

    def _format_codebook_section(self) -> str:
        codes = list(self.codebook.codes or [])
        if not codes:
            return "The codebook is currently empty. Create new codes as needed."
        codes_list = "\n".join(f"- {c.code}" for c in codes[:50])
        return f"Existing codes ({len(codes)} total):\n{codes_list}"

    def _build_user_prompt(self, segment: Segment) -> str:
        template = (
            self.coder_config.custom_prompts.user_prompt
            if self.coder_config.custom_prompts is not None
            else CODER_USER_PROMPT
        )
        return template.format(
            codebook_section=self._format_codebook_section(),
            segment_id=str(segment.segment_id),
            segment_text=segment.content,
        )

    def _parse_response(self, response: str, segment: Segment) -> list[Code]:
        """Parse the LLM JSON into transient ``Code`` rows with quotes.

        Truncates to ``max_codes_per_segment``. Each quote must appear as
        a substring of the segment — either literally or after a loose
        normalization (control chars stripped, whitespace collapsed,
        case-insensitive). Reuses any existing ``Quote`` on
        ``segment.quotes`` so we don't create duplicate quote rows.

        On missing or malformed JSON, or when ``codes`` is empty, returns
        a single sentinel ``Code``. When the LLM returns codes whose
        quotes all fail the verbatim check, raises
        ``QuoteVerificationError`` so the caller can ask for a revision.
        """
        json_str = extract_json_str(response)
        if json_str is None:
            return [self._sentinel(segment)]
        try:
            parsed = _CoderResponse.model_validate_json(json_str)
        except (json.JSONDecodeError, ValidationError):
            return [self._sentinel(segment)]

        # Track quotes created within this parse so codes that claim the
        # same (or near-identical) span share one Quote object.
        local_pool: list[Quote] = list(segment.quotes or [])
        local_seg = SimpleNamespace(
            segment_id=segment.segment_id,
            content=segment.content,
            quotes=local_pool,
        )
        normalized_segment = _normalize_for_match(segment.content)

        out: list[Code] = []
        bad_codes: list[tuple[str, list[str]]] = []
        for item in parsed.codes[: self.coder_config.max_codes_per_segment]:
            code_text = item.code.strip()
            if not code_text:
                continue
            quotes: list[Quote] = []
            seen: set[int] = set()
            rejected: list[str] = []
            for q in item.quotes:
                qt = q.strip()
                if not qt:
                    continue
                if qt in segment.content:
                    stored = qt
                elif _normalize_for_match(qt) and (
                    _normalize_for_match(qt) in normalized_segment
                ):
                    stored = qt
                else:
                    rejected.append(qt)
                    continue
                quote = create_quote(local_seg, stored)
                if id(quote) in seen:
                    continue
                seen.add(id(quote))
                quotes.append(quote)
                if quote.quote_id is None and quote not in local_pool:
                    local_pool.append(quote)
            if not quotes:
                bad_codes.append((code_text, rejected or list(item.quotes)))
                continue
            code = self._new_code(
                segment, code_text, item.description.strip()
            )
            code.supporting_quotes = quotes
            out.append(code)
        if bad_codes:
            raise QuoteVerificationError(bad_codes)
        return out or [self._sentinel(segment)]

    def _new_code(self, segment: Segment, code: str, description: str) -> Code:
        return Code(
            segment_id=segment.segment_id,
            coder_id=self.coder.coder_id,
            codebook_used_id=self.codebook.version,
            code=code,
            description=description,
        )

    def _sentinel(self, segment: Segment) -> Code:
        return self._new_code(segment, SENTINEL_CODE_LABEL, "")

    def _initial_coder_messages(self, segment: Segment) -> list[Message]:
        return [
            Message(
                role="system",
                content=[TextContent(text=self.get_system_prompt())],
            ),
            Message(
                role="user",
                content=[TextContent(text=self._build_user_prompt(segment))],
            ),
        ]

    def parse_with_retry(
        self,
        messages: list[Message],
        response_text: str,
        segment: Segment,
    ) -> list[Code]:
        """Parse a coder response, retrying once in-chat on bad quotes.

        On ``QuoteVerificationError`` the rejected quotes are echoed back
        to the LLM in a follow-up user turn (preserving the chat prefix
        for prompt caching), and the new response is parsed. If the
        second response also has bad quotes, the exception propagates.

        Records the retry on ``self.last_quote_retry`` so debug consumers
        (CLI ``test-code``) can show the follow-up prompt and the second
        response in full, even when the retry itself fails.
        """
        self.last_quote_retry = None
        try:
            return self._parse_response(response_text, segment)
        except QuoteVerificationError as exc:
            followup = _build_quote_retry_user_prompt(exc.bad_codes)
            retry_msgs = messages + [
                Message(
                    role="assistant",
                    content=[TextContent(text=response_text)],
                ),
                Message(
                    role="user", content=[TextContent(text=followup)]
                ),
            ]
            retry_response = self._completion_with_retry(
                retry_msgs, {"response_format": CODER_RESPONSE_SCHEMA}
            )
            retry_text = self._extract_text(retry_response)
            self.last_quote_retry = {
                "rejected_first_pass": exc.bad_codes,
                "followup_prompt": followup,
                "retry_response": retry_text,
            }
            return self._parse_response(retry_text, segment)

    async def parse_with_retry_async(
        self,
        messages: list[Message],
        response_text: str,
        segment: Segment,
    ) -> list[Code]:
        self.last_quote_retry = None
        try:
            return self._parse_response(response_text, segment)
        except QuoteVerificationError as exc:
            followup = _build_quote_retry_user_prompt(exc.bad_codes)
            retry_msgs = messages + [
                Message(
                    role="assistant",
                    content=[TextContent(text=response_text)],
                ),
                Message(
                    role="user", content=[TextContent(text=followup)]
                ),
            ]
            retry_response = await self._completion_with_retry_async(
                retry_msgs, {"response_format": CODER_RESPONSE_SCHEMA}
            )
            retry_text = self._extract_text(retry_response)
            self.last_quote_retry = {
                "rejected_first_pass": exc.bad_codes,
                "followup_prompt": followup,
                "retry_response": retry_text,
            }
            return self._parse_response(retry_text, segment)

    def code_segment(self, segment: Segment) -> list[Code]:
        messages = self._initial_coder_messages(segment)
        response = self._completion_with_retry(
            messages, {"response_format": CODER_RESPONSE_SCHEMA}
        )
        response_text = self._extract_text(response)
        return self.parse_with_retry(messages, response_text, segment)

    async def code_segment_async(self, segment: Segment) -> list[Code]:
        messages = self._initial_coder_messages(segment)
        response = await self._completion_with_retry_async(
            messages, {"response_format": CODER_RESPONSE_SCHEMA}
        )
        response_text = self._extract_text(response)
        return await self.parse_with_retry_async(
            messages, response_text, segment
        )

    def code_segments(self, segments: list[Segment]) -> list[list[Code]]:
        return [self.code_segment(seg) for seg in segments]

    async def code_segments_async(
        self, segments: list[Segment]
    ) -> list[list[Code]]:
        import asyncio

        tasks = [self.code_segment_async(seg) for seg in segments]
        return list(await asyncio.gather(*tasks))
