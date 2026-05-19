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
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.codebook import Codebook
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


@dataclass
class CodeAssignment:
    """Aggregator input: codes a single coder produced for one segment.

    Slim transport object used by ``CodeAggregatorAgent``. The coder
    itself returns transient ``Code`` instances; the worker layer
    builds this shape when handing per-coder code lists to the
    aggregator.
    """

    segment_id: str
    segment_text: str
    codes: list[str] = field(default_factory=list)


CODER_SYSTEM_PROMPT = """\
You are a coder in thematic analysis. When given a text segment,
write 1–3 codes for the segment. The code should capture concepts or
ideas with the most analytical interest, relevant to the research
focus.

For each code, provide a short description (one sentence) of what
the concept means as a general analytic category, and extract one or
more quotes from the segment corresponding to the code. Each quote
needs to be an extract from a sentence — copied verbatim from the
segment, not paraphrased.

When an existing code in the codebook fits, reuse its exact label.

If the segment does not address the research focus, return an empty
list of codes. Do not invent codes to cover off-topic material.

{identity_section}"""

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


CODER_USER_PROMPT = """\
## Current Codebook:
{codebook_section}

## Text Segment to Code:
ID: {segment_id}
Text: "{segment_text}"

{similar_codes_section}

Output codes following the required schema."""


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
            research_section = (
                "\n"
                + self.research_context.to_prompt_section(role="coder")
                + "\n"
            )

        base_prompt = CODER_SYSTEM_PROMPT
        if self.coder_config.custom_prompts is not None:
            base_prompt = self.coder_config.custom_prompts.system_prompt

        prompt = base_prompt.format(identity_section=identity_section)

        if research_section:
            prompt = research_section + "\n\n" + prompt

        return prompt

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

        - Truncates to ``max_codes_per_segment``.
        - Drops quotes that aren't a substring of ``segment_text``
          (best-effort verbatim check).
        - Drops a code if no surviving quotes remain.
        """
        json_match = re.search(r"```(?:json)?\s*(.*?)```", response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            json_match = re.search(r"\{.*\}", response, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                return None

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None

        items = data.get("codes")
        if not isinstance(items, list):
            return None

        out: list[Code] = []
        for item in items[: self.coder_config.max_codes_per_segment]:
            if not isinstance(item, dict):
                continue
            code_text = (item.get("code") or "").strip()
            description = (item.get("description") or "").strip()
            raw_quotes = item.get("quotes") or []
            if not code_text:
                continue
            quotes: list[Quote] = []
            seen: set[str] = set()
            for q in raw_quotes:
                if not isinstance(q, str):
                    continue
                qt = q.strip()
                if not qt or qt in seen:
                    continue
                if qt not in segment_text:
                    continue
                seen.add(qt)
                quotes.append(Quote(text=qt))
            if not quotes:
                continue
            code = Code(code=code_text, description=description)
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
