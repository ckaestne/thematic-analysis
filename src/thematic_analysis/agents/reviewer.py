"""Reviewer agent for maintaining and updating the adaptive codebook."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_response_json
from thematic_analysis.codebook import Codebook, CodeEntry, Quote
from thematic_analysis.prompts import join_system_prompt_sections

if TYPE_CHECKING:
    from thematic_analysis.research_context import ResearchContext


class ReviewDecision(Enum):
    """Decision types for code review."""

    ADD_NEW = "add_new"  # Add as a new code
    MERGE = "merge"  # Merge with existing code
    UPDATE = "update"  # Update existing code's description/name
    SKIP = "skip"  # Skip (duplicate or low quality)


@dataclass
class ReviewResult:
    """Result of reviewing a code against the codebook."""

    code: str
    decision: ReviewDecision
    target_code: str | None = None  # Code to merge with or update
    rationale: str = ""


@dataclass
class ReviewerConfig(AgentConfig):
    """Configuration for the Reviewer agent."""

    similarity_threshold: float = 0.75  # Threshold for considering codes similar
    top_k_similar: int = 5  # Number of similar codes to retrieve
    merge_threshold: float = 0.90  # Threshold for automatic merging


REVIEWER_SYSTEM_PROMPT = """\
You are an expert qualitative researcher responsible for maintaining the codebook.
Your task is to review new codes and decide how they should be integrated with
existing codes in the codebook.

## Your Responsibilities:
1. Compare new codes with existing similar codes
2. Decide whether codes should be merged, updated, or kept separate
3. Ensure the codebook remains consistent and well-organized
4. Preserve important analytical distinctions

## Decision Guidelines:
- **MERGE**: When codes capture the same concept with different wording
- **UPDATE**: When a new code is a better label for an existing concept
- **ADD_NEW**: When the code represents a genuinely new concept
- **SKIP**: When the code is a duplicate, lacks analytical value, or is
  off-topic relative to the research focus (if one is provided)

## Output Format:
Respond with a JSON object containing:
- "decision": One of "merge", "update", "add_new", or "skip"
- "target_code": The existing code to merge with/update (if applicable)
- "rationale": Brief explanation for the decision

Example:
```json
{{
  "decision": "merge",
  "target_code": "emotional support",
  "rationale": "Both codes describe receiving emotional assistance from others"
}}
```"""

REVIEWER_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_review_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["merge", "update", "add_new", "skip"],
                },
                "target_code": {"type": ["string", "null"]},
                "rationale": {"type": "string"},
            },
            "required": ["decision", "target_code", "rationale"],
        },
    },
}


REVIEWER_USER_PROMPT = """\
## New Code to Review:
Code: "{new_code}"
Quotes:
{quotes_section}

## Similar Existing Codes:
{similar_codes_section}

Please review this code and decide how it should be integrated into the codebook.
Provide your response as JSON."""


class ReviewerAgent(BaseAgent):
    """Agent that maintains and updates the adaptive codebook.

    The Reviewer Agent processes new codes from coders/aggregators,
    compares them with existing codes using semantic similarity,
    and decides whether to add, merge, update, or skip codes.
    """

    def __init__(
        self,
        config: ReviewerConfig | None = None,
        codebook: Codebook | None = None,
        research_context: ResearchContext | None = None,
    ):
        """Initialize the Reviewer agent.

        Args:
            config: Reviewer configuration.
            codebook: Initial codebook to maintain.
            research_context: Optional research context for scope-aware review.
        """
        super().__init__(config or ReviewerConfig())
        self.reviewer_config: ReviewerConfig = self.config  # type: ignore
        self.codebook = codebook if codebook is not None else Codebook()
        self.research_context = research_context

    def get_system_prompt(self) -> str:
        """Get the system prompt for review."""
        research_section = ""
        if self.research_context and not self.research_context.is_empty():
            research_section = self.research_context.to_prompt_section(
                role="reviewer"
            )
        return join_system_prompt_sections(
            REVIEWER_SYSTEM_PROMPT,
            research_context_instructions=research_section,
        )

    def _format_quotes_section(self, quotes: list[Quote]) -> str:
        """Format quotes for the prompt."""
        if not quotes:
            return "No quotes available."

        lines = []
        for q in quotes[:5]:  # Show up to 5 quotes
            text = q.text[:200] + "..." if len(q.text) > 200 else q.text
            lines.append(f'- [{q.quote_id}] "{text}"')
        return "\n".join(lines)

    def _format_similar_codes_section(
        self, similar_codes: list[tuple[CodeEntry, float]]
    ) -> str:
        """Format similar codes for the prompt."""
        if not similar_codes:
            return "No similar codes found in the codebook."

        lines = []
        for entry, score in similar_codes:
            quote_sample = ""
            if entry.quotes:
                quote_sample = f' (e.g., "{entry.quotes[0].text[:100]}...")'
            lines.append(f"- **{entry.code}** (similarity: {score:.2f}){quote_sample}")
        return "\n".join(lines)

    def _parse_response(self, response: str) -> tuple[ReviewDecision, str | None, str]:
        """Parse the LLM response into a review decision.

        Args:
            response: The raw LLM response.

        Returns:
            Tuple of (decision, target_code, rationale).
        """
        data = extract_response_json(response)
        if data is None:
            return ReviewDecision.ADD_NEW, None, "Could not parse response"

        decision_str = data.get("decision", "add_new").lower()
        target_code = data.get("target_code")
        rationale = data.get("rationale", "")

        decision_map = {
            "merge": ReviewDecision.MERGE,
            "update": ReviewDecision.UPDATE,
            "add_new": ReviewDecision.ADD_NEW,
            "skip": ReviewDecision.SKIP,
        }
        decision = decision_map.get(decision_str, ReviewDecision.ADD_NEW)

        return decision, target_code, rationale

    def review_code(self, code: str, quotes: list[Quote]) -> ReviewResult:
        """Review a single code against the codebook.

        Args:
            code: The code label to review.
            quotes: Associated quotes for the code (shown to the LLM as
                context; persistence is the worker's job).

        Returns:
            ReviewResult with the decision.
        """
        similar_codes = self.codebook.find_similar_codes(
            code, top_k=self.reviewer_config.top_k_similar
        )

        if similar_codes:
            top_entry, top_score = similar_codes[0]
            if top_score >= self.reviewer_config.merge_threshold:
                return ReviewResult(
                    code=code,
                    decision=ReviewDecision.MERGE,
                    target_code=top_entry.code,
                    rationale=f"Automatic merge: {top_score:.2f} similarity",
                )

        similar_above_threshold = [
            (entry, score)
            for entry, score in similar_codes
            if score >= self.reviewer_config.similarity_threshold
        ]

        if not similar_above_threshold:
            return ReviewResult(
                code=code,
                decision=ReviewDecision.ADD_NEW,
                rationale="No similar codes found",
            )

        user_prompt = REVIEWER_USER_PROMPT.format(
            new_code=code,
            quotes_section=self._format_quotes_section(quotes),
            similar_codes_section=self._format_similar_codes_section(
                similar_above_threshold
            ),
        )

        response = self._call_llm(
            self.get_system_prompt(),
            user_prompt,
            response_format=REVIEWER_RESPONSE_SCHEMA,
        )
        decision, target_code, rationale = self._parse_response(response)

        return ReviewResult(
            code=code,
            decision=decision,
            target_code=target_code,
            rationale=rationale,
        )
