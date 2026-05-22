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
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

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

        Truncates to ``max_codes_per_segment``, drops quotes that aren't a
        substring of the segment text (best-effort verbatim check), and
        drops a code if no surviving quotes remain. Reuses any existing
        ``Quote`` on ``segment.quotes`` with matching text so we don't
        create duplicate quote rows for the same segment. On missing or
        malformed JSON, or when no usable codes survived filtering,
        returns a single sentinel ``Code``.
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

        out: list[Code] = []
        for item in parsed.codes[: self.coder_config.max_codes_per_segment]:
            code_text = item.code.strip()
            if not code_text:
                continue
            quotes: list[Quote] = []
            seen: set[int] = set()
            for q in item.quotes:
                qt = q.strip()
                if not qt or qt not in segment.content:
                    continue
                quote = create_quote(local_seg, qt)
                if id(quote) in seen:
                    continue
                seen.add(id(quote))
                quotes.append(quote)
                if quote.quote_id is None and quote not in local_pool:
                    local_pool.append(quote)
            if not quotes:
                continue
            code = self._new_code(
                segment, code_text, item.description.strip()
            )
            code.supporting_quotes = quotes
            out.append(code)
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

    def code_segment(self, segment: Segment) -> list[Code]:
        response = self._call_llm(
            self.get_system_prompt(),
            self._build_user_prompt(segment),
            response_format=CODER_RESPONSE_SCHEMA,
        )
        return self._parse_response(response, segment)

    async def code_segment_async(self, segment: Segment) -> list[Code]:
        response = await self._call_llm_async(
            self.get_system_prompt(),
            self._build_user_prompt(segment),
            response_format=CODER_RESPONSE_SCHEMA,
        )
        return self._parse_response(response, segment)

    def code_segments(self, segments: list[Segment]) -> list[list[Code]]:
        return [self.code_segment(seg) for seg in segments]

    async def code_segments_async(
        self, segments: list[Segment]
    ) -> list[list[Code]]:
        import asyncio

        tasks = [self.code_segment_async(seg) for seg in segments]
        return list(await asyncio.gather(*tasks))
