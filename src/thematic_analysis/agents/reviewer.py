"""Reviewer agent for maintaining and updating the adaptive codebook."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_response_json
from thematic_analysis.codebook import Codebook, CodeEntry, Quote
from thematic_analysis.prompts import join_system_prompt_sections


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
    top_k_similar: int = 10  # Number of similar codes to retrieve (paper §4)
    merge_threshold: float = 0.90  # Threshold for automatic merging
    max_quotes_per_code: int = 5  # Quotes shown per code in the prompt


REVIEWER_SYSTEM_PROMPT = """\
You are an expert qualitative researcher responsible for maintaining the codebook.
Your task is to review a new code against similar existing codes and decide
how it should be integrated.

## Input
The user message is a JSON object:
- `new_code`: an object with `code` (the new label) and `quotes` (the verbatim
  quote texts that support it).
- `similar_codes`: a list of existing codes that may overlap. Each entry has
  `code` (the existing label), `similarity` (cosine similarity to the new code),
  and `quotes` (the verbatim quote texts already associated with it).

## Decision Guidelines
- **merge**: the new code captures the same concept as an existing one — just
  with different wording. Set `target_code` to that existing code's label.
- **update**: the new code is a clearly better label for an existing concept.
  Set `target_code` to the existing code's label that should be renamed.
- **add_new**: the code represents a genuinely new concept.
- **skip**: the code is a duplicate without new information, lacks analytical
  value, or is off-topic relative to the research focus (if one is provided).

## Output Format
Respond with a single JSON object:
- `decision`: one of "merge", "update", "add_new", or "skip"
- `target_code`: the existing code label to merge with / rename (for merge or
  update). Use the exact label from the input. `null` for add_new and skip.
- `rationale`: brief explanation for the decision."""

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
    ):
        super().__init__(config or ReviewerConfig())
        self.reviewer_config: ReviewerConfig = self.config  # type: ignore
        self.codebook = codebook if codebook is not None else Codebook()
        # Debug fields populated by the most recent review_code call so
        # callers (e.g. the `test-review` CLI) can inspect what the agent
        # did without re-implementing prompt building or the LLM call.
        # The short-circuit paths (auto-merge, no-similar-codes) leave
        # these empty to signal "no LLM call".
        self.last_similar_codes: list[tuple[CodeEntry, float]] = []
        self.last_payload: dict | None = None
        self.last_system_prompt: str = ""
        self.last_user_prompt: str = ""
        self.last_raw_response: str = ""
        self.last_elapsed: float = 0.0
        self.last_shortcut: str | None = None  # "auto_merge" | "no_similar" | None

    def get_system_prompt(self) -> str:
        rc = self.codebook.research_context
        research_section = ""
        if rc is not None and not rc.is_empty():
            research_section = rc.to_prompt_section(role="reviewer")
        return join_system_prompt_sections(
            REVIEWER_SYSTEM_PROMPT,
            research_context_instructions=research_section,
        )

    def _build_payload(
        self,
        code: str,
        quotes: list[Quote],
        similar_codes: list[tuple[CodeEntry, float]],
    ) -> dict:
        n = self.reviewer_config.max_quotes_per_code
        return {
            "new_code": {
                "code": code,
                "quotes": [q.text for q in quotes[:n]],
            },
            "similar_codes": [
                {
                    "code": entry.code,
                    "similarity": round(score, 3),
                    "quotes": [q.text for q in entry.quotes[:n]],
                }
                for entry, score in similar_codes
            ],
        }

    def _parse_response(self, response: str) -> tuple[ReviewDecision, str | None, str]:
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
        self.last_similar_codes = []
        self.last_payload = None
        self.last_system_prompt = ""
        self.last_user_prompt = ""
        self.last_raw_response = ""
        self.last_elapsed = 0.0
        self.last_shortcut = None

        similar_codes = self.codebook.find_similar_codes(
            code, top_k=self.reviewer_config.top_k_similar
        )
        self.last_similar_codes = similar_codes

        if similar_codes:
            top_entry, top_score = similar_codes[0]
            if top_score >= self.reviewer_config.merge_threshold:
                self.last_shortcut = "auto_merge"
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
            self.last_shortcut = "no_similar"
            return ReviewResult(
                code=code,
                decision=ReviewDecision.ADD_NEW,
                rationale="No similar codes found",
            )

        payload = self._build_payload(code, quotes, similar_above_threshold)
        system_prompt = self.get_system_prompt()
        user_prompt = json.dumps(payload, indent=2)
        self.last_payload = payload
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt

        t0 = time.monotonic()
        response = self._call_llm(
            system_prompt,
            user_prompt,
            response_format=REVIEWER_RESPONSE_SCHEMA,
        )
        self.last_raw_response = response
        self.last_elapsed = time.monotonic() - t0
        decision, target_code, rationale = self._parse_response(response)

        return ReviewResult(
            code=code,
            decision=decision,
            target_code=target_code,
            rationale=rationale,
        )
