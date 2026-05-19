"""Coder agent for assigning codes to text segments."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.codebook import Codebook


if TYPE_CHECKING:
    from thematic_analysis.prompts import CoderPrompts
    from thematic_analysis.research_context import ResearchContext


@dataclass
class CoderConfig(AgentConfig):
    """Configuration for the Coder agent."""

    max_codes_per_segment: int = 5
    similarity_threshold: float = 0.7
    include_rationale: bool = True  # Chain-of-thought for better alignment
    include_6rs_guidance: bool = True  # Include 6Rs code quality criteria
    custom_prompts: CoderPrompts | None = None  # Custom prompts (Issue #38)


@dataclass
class CodeAssignment:
    """Result of coding a text segment."""

    segment_id: str
    segment_text: str
    codes: list[str]
    quotes: list[str] = field(default_factory=list)
    rationales: list[str] = field(default_factory=list)
    is_new_code: list[bool] = field(default_factory=list)


CODER_SYSTEM_PROMPT = """\
You are an expert qualitative researcher performing thematic coding.
Your task is to analyze text segments and assign meaningful codes
that capture the key concepts, themes, and patterns **relevant to the
research focus**.

## Guidelines for Coding:
1. **Read carefully**: Understand the full meaning and context of the text
2. **Stay on topic**: Only code content that speaks to the research focus.
   Source texts often range broadly; ignore unrelated material rather than
   stretching codes to cover it.
3. **Identify key concepts**: Look for important ideas, experiences, or
   patterns relevant to the research questions
4. **Create descriptive codes**: Codes should be concise but meaningful labels
5. **Consider existing codes**: When possible, use or adapt existing codes
6. **Be consistent**: Apply codes consistently across similar content
7. **Extract representative quotes**: For each code, copy a short, verbatim
   excerpt from the text that best illustrates why the code applies. The quote
   must appear word-for-word in the text.

## Code Quality Criteria (6 Rs):
- **Reciprocal**: Codes should relate meaningfully to the data
- **Recognizable**: Codes should be clear and understandable
- **Responsive**: Codes should address the research questions
- **Resourceful**: Codes should capture nuanced meanings

## Off-topic Segments:
If a segment is wholly unrelated to the research focus, return an empty
"codes" list, an empty "quotes" list, and a single rationale starting with
"OUT_OF_SCOPE:" briefly explaining why. Do NOT invent codes to cover
off-topic material. If only part of the segment is relevant, code only that
part and ignore the rest. Codes must be grounded in content that addresses
the research focus — not in tangential or background material.

{identity_section}

## Output Format:
Respond with a JSON object containing:
- "codes": List of code labels assigned to this segment
- "quotes": List of short verbatim excerpts from the text, one per code
- "rationales": List of brief explanations for each code assignment
- "is_new": List of booleans indicating if each code is new (not in codebook)

Example:
```json
{{
  "codes": ["emotional support", "peer connection"],
  "quotes": ["felt comforted by my friends", "built strong bonds with peers"],
  "rationales": ["Comfort from others", "Building peer relationships"],
  "is_new": [false, true]
}}
```"""

CODER_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_assignment",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "codes": {"type": "array", "items": {"type": "string"}},
                "quotes": {"type": "array", "items": {"type": "string"}},
                "rationales": {"type": "array", "items": {"type": "string"}},
                "is_new": {"type": "array", "items": {"type": "boolean"}},
            },
            "required": ["codes", "quotes", "rationales", "is_new"],
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

First check whether this segment relates to the research focus. If it does
not, return an empty "codes" list with a single rationale starting with
"OUT_OF_SCOPE:". Otherwise assign codes only to the parts that address the
research focus. Provide your response as JSON."""


class CoderAgent(BaseAgent):
    """Agent that assigns codes to text segments.

    The Coder agent analyzes text and assigns codes from an existing codebook
    or creates new codes when needed. It uses semantic similarity to find
    relevant existing codes.

    Supports methodology-aware coding with research context (Naeem et al. 2025).
    """

    def __init__(
        self,
        config: CoderConfig | None = None,
        codebook: Codebook | None = None,
        research_context: ResearchContext | None = None,
    ):
        """Initialize the Coder agent.

        Args:
            config: Coder configuration.
            codebook: Initial codebook to use. Created if not provided.
            research_context: Research context for methodology-aware coding.
        """
        super().__init__(config or CoderConfig())
        self.coder_config: CoderConfig = self.config  # type: ignore
        self.codebook = codebook if codebook is not None else Codebook()
        self.research_context = research_context

    def set_research_context(self, context: ResearchContext) -> None:
        """Set or update the research context.

        Args:
            context: The research context to use for coding.
        """
        self.research_context = context

    def get_system_prompt(self) -> str:
        """Get the system prompt with optional identity and research context."""
        identity_section = ""
        if self.config.identity:
            identity_section = f"""
## Your Perspective:
You are coding from the following perspective: {self.config.identity}
Let this perspective inform how you interpret and code the data, while maintaining
analytical rigor and staying grounded in the text."""

        # Add research context if available
        research_section = ""
        if self.research_context and not self.research_context.is_empty():
            research_section = (
                "\n"
                + self.research_context.to_prompt_section(role="coder")
                + "\n"
            )

        # Use custom prompts if provided (Issue #38), otherwise use default
        base_prompt = CODER_SYSTEM_PROMPT
        if self.coder_config.custom_prompts is not None:
            base_prompt = self.coder_config.custom_prompts.system_prompt

        prompt = base_prompt.format(identity_section=identity_section)

        if research_section:
            prompt = research_section + "\n\n" + prompt

        return prompt

    def _get_user_prompt_template(self) -> str:
        """Get the user prompt template.

        Returns custom template if configured, otherwise default.
        """
        if self.coder_config.custom_prompts is not None:
            return self.coder_config.custom_prompts.user_prompt
        return CODER_USER_PROMPT

    def _format_codebook_section(self) -> str:
        """Format the current codebook for the prompt."""
        if len(self.codebook) == 0:
            return "The codebook is currently empty. Create new codes as needed."

        codes_list = "\n".join(
            f"- {entry.code}" for entry in self.codebook.entries[:50]
        )
        return f"Existing codes ({len(self.codebook)} total):\n{codes_list}"

    def _format_similar_codes_section(self, text: str) -> str:
        """Find and format similar codes for the given text."""
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

    def _parse_response(self, response: str, segment_id: str) -> CodeAssignment | None:
        """Parse the LLM response into a CodeAssignment.

        Args:
            response: The raw LLM response.
            segment_id: The ID of the segment being coded.

        Returns:
            CodeAssignment if parsing succeeds, None otherwise.
        """
        # Extract JSON from response (handle markdown code blocks)
        json_match = re.search(r"```(?:json)?\s*(.*?)```", response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            # Try to find raw JSON object
            json_match = re.search(r"\{.*\}", response, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                return None

        try:
            data = json.loads(json_str)
            codes = data.get("codes", [])
            quotes = data.get("quotes", [])
            rationales = data.get("rationales", [])
            is_new = data.get("is_new", [True] * len(codes))

            # Ensure lists are the same length as codes
            while len(quotes) < len(codes):
                quotes.append("")
            while len(rationales) < len(codes):
                rationales.append("")
            while len(is_new) < len(codes):
                is_new.append(True)

            max_n = self.coder_config.max_codes_per_segment
            return CodeAssignment(
                segment_id=segment_id,
                segment_text="",  # Will be filled by caller
                codes=codes[:max_n],
                quotes=quotes[:max_n],
                rationales=rationales[:max_n],
                is_new_code=is_new[:max_n],
            )
        except json.JSONDecodeError:
            return None

    def _build_user_prompt(self, segment_id: str, text: str) -> str:
        """Build the user prompt for coding a segment.

        Args:
            segment_id: Unique identifier for the segment.
            text: The text to code.

        Returns:
            Formatted user prompt string.
        """
        template = self._get_user_prompt_template()
        return template.format(
            codebook_section=self._format_codebook_section(),
            segment_id=segment_id,
            segment_text=text,
            similar_codes_section=self._format_similar_codes_section(text),
        )

    def _process_response(
        self, response: str, segment_id: str, text: str
    ) -> CodeAssignment:
        """Process LLM response into a CodeAssignment.

        Args:
            response: The LLM response text.
            segment_id: The segment ID.
            text: The segment text.

        Returns:
            CodeAssignment with assigned codes.
        """
        assignment = self._parse_response(response, segment_id)

        if assignment is None:
            # Fallback: return empty assignment
            assignment = CodeAssignment(
                segment_id=segment_id,
                segment_text=text,
                codes=[],
                quotes=[],
                rationales=[],
                is_new_code=[],
            )
        else:
            assignment.segment_text = text
            # Replace empty/missing quotes with the full segment text as fallback
            assignment.quotes = [
                q if q else text for q in assignment.quotes
            ]
            while len(assignment.quotes) < len(assignment.codes):
                assignment.quotes.append(text)

        return assignment

    def code_segment(self, segment_id: str, text: str) -> CodeAssignment:
        """Code a single text segment (synchronous).

        Args:
            segment_id: Unique identifier for the segment.
            text: The text to code.

        Returns:
            CodeAssignment with assigned codes.
        """
        user_prompt = self._build_user_prompt(segment_id, text)
        response = self._call_llm(
            self.get_system_prompt(),
            user_prompt,
            response_format=CODER_RESPONSE_SCHEMA,
        )
        return self._process_response(response, segment_id, text)

    async def code_segment_async(self, segment_id: str, text: str) -> CodeAssignment:
        """Code a single text segment (asynchronous).

        Args:
            segment_id: Unique identifier for the segment.
            text: The text to code.

        Returns:
            CodeAssignment with assigned codes.
        """
        user_prompt = self._build_user_prompt(segment_id, text)
        response = await self._call_llm_async(
            self.get_system_prompt(),
            user_prompt,
            response_format=CODER_RESPONSE_SCHEMA,
        )
        return self._process_response(response, segment_id, text)

    def code_segments(self, segments: list[tuple[str, str]]) -> list[CodeAssignment]:
        """Code multiple text segments (synchronous).

        Note: Per the paper's architecture (Figure 2), coders produce assignments
        which flow to the Aggregator, then Reviewer. Codebook updates should
        happen through the ReviewerAgent, not here.

        Args:
            segments: List of (segment_id, text) tuples.

        Returns:
            List of CodeAssignments.
        """
        return [self.code_segment(seg_id, text) for seg_id, text in segments]

    async def code_segments_async(
        self, segments: list[tuple[str, str]]
    ) -> list[CodeAssignment]:
        """Code multiple text segments (asynchronous, parallel).

        Runs all segment coding in parallel using asyncio.gather.

        Args:
            segments: List of (segment_id, text) tuples.

        Returns:
            List of CodeAssignments.
        """
        import asyncio

        tasks = [self.code_segment_async(seg_id, text) for seg_id, text in segments]
        return list(await asyncio.gather(*tasks))
