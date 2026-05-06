"""Code Aggregator agent for merging codes from multiple coders."""

import json
import re
from dataclasses import dataclass

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.coder import CodeAssignment
from thematic_analysis.codebook import Codebook, Quote
from thematic_analysis.iterative import NegotiationStrategy, negotiate


@dataclass
class AggregatorConfig(AgentConfig):
    """Configuration for the Code Aggregator agent."""

    similarity_threshold: float = 0.8
    max_quotes_per_code: int = 10
    negotiation_strategy: NegotiationStrategy = NegotiationStrategy.CONSENSUS
    consensus_threshold: float = 0.5  # Fraction of coders needed for consensus


@dataclass
class MergedCode:
    """A code that may combine multiple similar codes."""

    code: str
    original_codes: list[str]
    quotes: list[Quote]
    merge_rationale: str = ""


@dataclass
class AggregationResult:
    """Result of aggregating codes from multiple coders."""

    merged_codes: list[MergedCode]
    retained_codes: list[MergedCode]  # Codes kept separate (different concepts)

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "merged_codes": [
                {
                    "code": mc.code,
                    "original_codes": mc.original_codes,
                    "quotes": [
                        {"quote_id": q.quote_id, "text": q.text} for q in mc.quotes
                    ],
                    "merge_rationale": mc.merge_rationale,
                }
                for mc in self.merged_codes
            ],
            "retained_codes": [
                {
                    "code": rc.code,
                    "original_codes": rc.original_codes,
                    "quotes": [
                        {"quote_id": q.quote_id, "text": q.text} for q in rc.quotes
                    ],
                }
                for rc in self.retained_codes
            ],
        }

    def to_json(self) -> str:
        """Convert to structured JSON format."""
        return json.dumps(self.to_dict(), indent=2)

    def all_codes(self) -> list[MergedCode]:
        """Return all codes (merged and retained)."""
        return self.merged_codes + self.retained_codes


AGGREGATOR_SYSTEM_PROMPT = """\
You are an expert qualitative researcher responsible for organizing codes from
multiple coders. Your task is to identify codes with similar meanings that should
be merged, while retaining codes that represent distinct concepts.

## Guidelines for Code Aggregation:
1. **Semantic similarity**: Merge codes that capture the same underlying concept
2. **Preserve nuance**: Keep codes separate if they capture different aspects
3. **Create clear labels**: When merging, create a clear label for the merged code
4. **Prioritize relevance**: Select the most representative quotes for each code

## Decision Criteria for Merging:
- Merge if: Codes describe the same phenomenon from different angles
- Merge if: One code is a more specific version of another
- Keep separate if: Codes capture different aspects of the data
- Keep separate if: Merging would lose important analytical distinctions

## Output Format:
Respond with a JSON object containing two lists:
- "merge_groups": List of code groups to merge, each with:
  - "merged_code": The new unified code label
  - "original_codes": List of original codes being merged
  - "rationale": Brief explanation for the merge
- "retain_codes": List of codes to keep separate (as-is)

Example:
```json
{{
  "merge_groups": [
    {{
      "merged_code": "emotional support from peers",
      "original_codes": ["peer support", "friend comfort", "emotional help"],
      "rationale": "All describe emotional assistance from peer relationships"
    }}
  ],
  "retain_codes": ["academic pressure", "time management"]
}}
```"""

AGGREGATOR_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_aggregation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "merge_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "merged_code": {"type": "string"},
                            "original_codes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "rationale": {"type": "string"},
                        },
                        "required": ["merged_code", "original_codes", "rationale"],
                    },
                },
                "retain_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["merge_groups", "retain_codes"],
        },
    },
}


AGGREGATOR_USER_PROMPT = """\
## Codes to Aggregate:
{codes_section}

## Similar Code Groups (based on semantic similarity):
{similar_groups_section}

Please analyze these codes and determine which should be merged and which should
remain separate. Provide your response as JSON."""


class CodeAggregatorAgent(BaseAgent):
    """Agent that merges and organizes codes from multiple coders.

    The Code Aggregator takes code assignments from multiple coder agents,
    identifies codes with similar meanings, merges them when appropriate,
    and organizes the results with top-K most relevant quotes.
    """

    def __init__(
        self,
        config: AggregatorConfig | None = None,
        codebook: Codebook | None = None,
    ):
        """Initialize the Code Aggregator agent.

        Args:
            config: Aggregator configuration.
            codebook: Codebook for semantic similarity search.
        """
        super().__init__(config or AggregatorConfig())
        self.aggregator_config: AggregatorConfig = self.config  # type: ignore
        self.codebook = codebook if codebook is not None else Codebook()

    def get_system_prompt(self) -> str:
        """Get the system prompt for aggregation."""
        return AGGREGATOR_SYSTEM_PROMPT

    def _apply_negotiation_strategy(
        self, assignments: list[CodeAssignment]
    ) -> set[str]:
        """Apply negotiation strategy to filter codes based on coder agreement.

        This integrates the NegotiationStrategy from iterative.py module.
        Per the paper: codes should be agreed upon by coders before aggregation.

        Args:
            assignments: List of code assignments from coders.

        Returns:
            Set of codes that pass the negotiation strategy filter.
        """
        # Build code sets per coder
        code_sets = [set(a.codes) for a in assignments]

        if not code_sets:
            return set()

        # Use the negotiate function from iterative module
        result = negotiate(
            code_sets=code_sets,
            strategy=self.aggregator_config.negotiation_strategy,
        )

        return set(result.agreed_codes)

    def _collect_codes_with_quotes(
        self, assignments: list[CodeAssignment], agreed_codes: set[str] | None = None
    ) -> dict[str, list[Quote]]:
        """Collect codes with their associated quotes.

        Args:
            assignments: List of code assignments from coders.
            agreed_codes: Optional set of codes that passed negotiation.
                         If None, all codes are included.

        Returns:
            Dict mapping code labels to lists of quotes.
        """
        code_quotes: dict[str, list[Quote]] = {}

        for assignment in assignments:
            for code in assignment.codes:
                # Filter by agreed codes if negotiation was applied
                if agreed_codes is not None and code not in agreed_codes:
                    continue

                if code not in code_quotes:
                    code_quotes[code] = []
                quote = Quote(
                    quote_id=assignment.segment_id,
                    text=assignment.segment_text,
                )
                # Avoid duplicate quotes
                if not any(q.quote_id == quote.quote_id for q in code_quotes[code]):
                    code_quotes[code].append(quote)

        return code_quotes

    def _find_similar_groups(self, codes: list[str]) -> list[list[str]]:
        """Group codes by semantic similarity.

        Args:
            codes: List of code labels.

        Returns:
            List of code groups that are semantically similar.
        """
        if len(codes) <= 1:
            return [codes] if codes else []

        # Build similarity matrix and group codes
        groups: list[list[str]] = []
        remaining = set(codes)

        for code in codes:
            if code not in remaining:
                continue

            remaining.remove(code)
            group = [code]

            # Find similar codes
            for other_code in list(remaining):
                similarity = self.codebook.embedding_service.compute_similarity(
                    code, other_code
                )
                if similarity >= self.aggregator_config.similarity_threshold:
                    group.append(other_code)
                    remaining.remove(other_code)

            groups.append(group)

        return groups

    def _format_codes_section(self, code_quotes: dict[str, list[Quote]]) -> str:
        """Format codes and quotes for the prompt."""
        lines = []
        for code, quotes in code_quotes.items():
            quote_samples = quotes[:3]  # Show up to 3 sample quotes
            quote_text = "; ".join(f'"{q.text[:100]}..."' for q in quote_samples)
            lines.append(f"- **{code}** ({len(quotes)} quotes): {quote_text}")
        return "\n".join(lines)

    def _format_similar_groups_section(self, groups: list[list[str]]) -> str:
        """Format similar code groups for the prompt."""
        if not groups:
            return "No similar groups identified."

        lines = []
        for i, group in enumerate(groups, 1):
            if len(group) > 1:
                lines.append(f"Group {i}: {', '.join(group)}")
            else:
                lines.append(f"Standalone: {group[0]}")
        return "\n".join(lines)

    def _parse_response(
        self,
        response: str,
        code_quotes: dict[str, list[Quote]],
    ) -> AggregationResult | None:
        """Parse the LLM response into an AggregationResult.

        Args:
            response: The raw LLM response.
            code_quotes: Dict mapping codes to quotes.

        Returns:
            AggregationResult if parsing succeeds, None otherwise.
        """
        # Extract JSON from response
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
            merged_codes = []
            retained_codes = []

            # Process merge groups
            for group in data.get("merge_groups", []):
                merged_code_label = group.get("merged_code", "")
                original_codes = group.get("original_codes", [])
                rationale = group.get("rationale", "")

                # Collect quotes from all original codes
                quotes = []
                for orig_code in original_codes:
                    if orig_code in code_quotes:
                        quotes.extend(code_quotes[orig_code])

                # Deduplicate and limit quotes
                seen_ids = set()
                unique_quotes = []
                for q in quotes:
                    if q.quote_id not in seen_ids:
                        seen_ids.add(q.quote_id)
                        unique_quotes.append(q)

                max_quotes = self.aggregator_config.max_quotes_per_code
                merged_codes.append(
                    MergedCode(
                        code=merged_code_label,
                        original_codes=original_codes,
                        quotes=unique_quotes[:max_quotes],
                        merge_rationale=rationale,
                    )
                )

            # Process retained codes
            for code_label in data.get("retain_codes", []):
                if code_label in code_quotes:
                    quotes = code_quotes[code_label]
                    retained_codes.append(
                        MergedCode(
                            code=code_label,
                            original_codes=[code_label],
                            quotes=quotes[: self.aggregator_config.max_quotes_per_code],
                        )
                    )

            return AggregationResult(
                merged_codes=merged_codes,
                retained_codes=retained_codes,
            )

        except json.JSONDecodeError:
            return None

    def aggregate(
        self,
        assignments: list[CodeAssignment],
        apply_negotiation: bool = True,
    ) -> AggregationResult:
        """Aggregate codes from multiple coder assignments.

        First applies NegotiationStrategy to filter codes by coder agreement,
        then merges semantically similar codes using LLM.

        Args:
            assignments: List of code assignments from coder agents.
            apply_negotiation: Whether to apply negotiation strategy first.
                             Set to False for backward compatibility.

        Returns:
            AggregationResult with merged and retained codes.
        """
        # Step 1: Apply negotiation strategy to filter codes
        agreed_codes = None
        if apply_negotiation and len(assignments) > 1:
            agreed_codes = self._apply_negotiation_strategy(assignments)
            # If negotiation yields no codes (e.g., INTERSECTION with no overlap),
            # fall back to UNION to avoid empty results
            if not agreed_codes:
                agreed_codes = None  # Include all codes

        # Step 2: Collect codes with their quotes (filtered by negotiation)
        code_quotes = self._collect_codes_with_quotes(assignments, agreed_codes)

        if not code_quotes:
            return AggregationResult(merged_codes=[], retained_codes=[])

        # Step 3: Find similar code groups using embeddings
        similar_groups = self._find_similar_groups(list(code_quotes.keys()))

        # Step 4: Use LLM to decide on merging
        user_prompt = AGGREGATOR_USER_PROMPT.format(
            codes_section=self._format_codes_section(code_quotes),
            similar_groups_section=self._format_similar_groups_section(similar_groups),
        )

        response = self._call_llm(
            self.get_system_prompt(),
            user_prompt,
            response_format=AGGREGATOR_RESPONSE_SCHEMA,
        )
        result = self._parse_response(response, code_quotes)

        if result is None:
            # Fallback: keep all codes separate
            retained = [
                MergedCode(
                    code=code,
                    original_codes=[code],
                    quotes=quotes[: self.aggregator_config.max_quotes_per_code],
                )
                for code, quotes in code_quotes.items()
            ]
            return AggregationResult(merged_codes=[], retained_codes=retained)

        return result

    def update_codebook(self, result: AggregationResult) -> Codebook:
        """Update the codebook with aggregated codes.

        Args:
            result: The aggregation result.

        Returns:
            Updated codebook.
        """
        # Clear existing codes and add aggregated ones
        new_codebook = Codebook(embedding_service=self.codebook.embedding_service)

        for merged_code in result.all_codes():
            new_codebook.add_code(merged_code.code, merged_code.quotes)

        return new_codebook
