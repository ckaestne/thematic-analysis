"""Coder agent for assigning codes to text segments.

Following Thematic-LM (Sec. 3 and Appendix "Main Prompts"), the coder
writes 1–3 codes per segment. For each code, it provides a short
description of the analytic concept and extracts one or more verbatim
quotes from the segment as evidence.

The agent returns transient (un-persisted) ``Code`` SQLModel instances
with attached ``Quote`` instances on ``supporting_quotes``. Persistence
(segment_id, coder_id, codebook version, etc.) is the caller's
responsibility.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_json_str
from thematic_analysis.codebook import Codebook
from thematic_analysis.prompts import (
    CODER_SYSTEM_PROMPT,
    CODER_USER_PROMPT,
    join_system_prompt_sections,
)
from thematic_analysis_inc.db.models import Code, Quote


if TYPE_CHECKING:
    from thematic_analysis.prompts import CoderPrompts
    from thematic_analysis.research_context import ResearchContext


@dataclass
class CoderConfig(AgentConfig):
    """Configuration for the Coder agent."""

    max_codes_per_segment: int = 5
    similarity_threshold: float = 0.7
    include_rationale: bool = True  # kept for backwards-compatible config
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

    Returns a list of transient ``Code`` instances with attached
    transient ``Quote`` instances (via ``code.supporting_quotes``).
    """

    def __init__(
        self,
        config: CoderConfig | None = None,
        codebook: Codebook | None = None,
        research_context: ResearchContext | None = None,
    ):
        super().__init__(config or CoderConfig())
        self.coder_config: CoderConfig = self.config  # type: ignore
        self.codebook = codebook if codebook is not None else Codebook()
        self.research_context = research_context

    def set_research_context(self, context: ResearchContext) -> None:
        self.research_context = context

    def get_system_prompt(self) -> str:
        identity_section = ""
        if self.config.identity:
            identity_section = f"""
## Your Perspective:
You are coding from the following perspective: {self.config.identity}
Let this perspective inform how you interpret and code the data, while maintaining
analytical rigor and staying grounded in the text."""

        research_section = ""
        if self.research_context and not self.research_context.is_empty():
            research_section = "## Research context\n" + self.research_context.to_prompt_section(
                role="coder"
            )

        base_prompt = CODER_SYSTEM_PROMPT
        if self.coder_config.custom_prompts is not None:
            base_prompt = self.coder_config.custom_prompts.system_prompt
        return join_system_prompt_sections(
            base_prompt,
            research_context_instructions=research_section,
            identity_instructions=identity_section,
        )

    def _get_user_prompt_template(self) -> str:
        if self.coder_config.custom_prompts is not None:
            return self.coder_config.custom_prompts.user_prompt
        return CODER_USER_PROMPT

    def _format_codebook_section(self) -> str:
        if len(self.codebook) == 0:
            return "The codebook is currently empty. Create new codes as needed."

        codes_list = "\n".join(
            f"- {entry.code}" for entry in self.codebook.entries[:50]
        )
        return f"Existing codes ({len(self.codebook)} total):\n{codes_list}"

    def _format_similar_codes_section(self, text: str) -> str:
        if len(self.codebook) == 0:
            return ""

        similar = self.codebook.find_similar_codes(text, top_k=5)
        if not similar:
            return ""

        similar_list = "\n".join(
            f"- {entry.code} (similarity: {score:.2f})"
            for entry, score in similar
            if score >= self.coder_config.similarity_threshold
        )

        if not similar_list:
            return ""

        return (
            "## Similar Existing Codes:\n"
            f"Consider using these relevant codes:\n{similar_list}"
        )

    def _parse_response(
        self, response: str, segment_text: str
    ) -> list[Code] | None:
        """Parse the LLM JSON into transient ``Code`` rows with quotes.

        Uses pydantic to validate the response shape. Truncates to
        ``max_codes_per_segment``, drops quotes that aren't a substring
        of ``segment_text`` (best-effort verbatim check), and drops a
        code if no surviving quotes remain.
        """
        json_str = extract_json_str(response)
        if json_str is None:
            return None

        try:
            parsed = _CoderResponse.model_validate_json(json_str)
        except (json.JSONDecodeError, ValidationError):
            return None

        out: list[Code] = []
        for item in parsed.codes[: self.coder_config.max_codes_per_segment]:
            code_text = item.code.strip()
            if not code_text:
                continue
            quotes: list[Quote] = []
            seen: set[str] = set()
            for q in item.quotes:
                qt = q.strip()
                if not qt or qt in seen or qt not in segment_text:
                    continue
                seen.add(qt)
                quotes.append(Quote(text=qt))
            if not quotes:
                continue
            code = Code(code=code_text, description=item.description.strip())
            code.supporting_quotes = quotes
            out.append(code)
        return out

    def _build_user_prompt(self, segment_id: str, text: str) -> str:
        template = self._get_user_prompt_template()
        return template.format(
            codebook_section=self._format_codebook_section(),
            segment_id=segment_id,
            segment_text=text,
            similar_codes_section=self._format_similar_codes_section(text),
        )

    def _process_response(self, response: str, text: str) -> list[Code]:
        parsed = self._parse_response(response, text)
        return parsed if parsed is not None else []

    def code_segment(self, segment_id: str, text: str) -> list[Code]:
        user_prompt = self._build_user_prompt(segment_id, text)
        response = self._call_llm(
            self.get_system_prompt(),
            user_prompt,
            response_format=CODER_RESPONSE_SCHEMA,
        )
        return self._process_response(response, text)

    async def code_segment_async(self, segment_id: str, text: str) -> list[Code]:
        user_prompt = self._build_user_prompt(segment_id, text)
        response = await self._call_llm_async(
            self.get_system_prompt(),
            user_prompt,
            response_format=CODER_RESPONSE_SCHEMA,
        )
        return self._process_response(response, text)

    def code_segments(self, segments: list[tuple[str, str]]) -> list[list[Code]]:
        return [self.code_segment(seg_id, text) for seg_id, text in segments]

    async def code_segments_async(
        self, segments: list[tuple[str, str]]
    ) -> list[list[Code]]:
        import asyncio

        tasks = [self.code_segment_async(seg_id, text) for seg_id, text in segments]
        return list(await asyncio.gather(*tasks))
