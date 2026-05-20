"""Theme Aggregator agent for merging themes from multiple theme coders."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.json_utils import extract_response_json
from thematic_analysis.agents.theme_coder import Theme, ThemeResult
from thematic_analysis.codebook import EmbeddingService, Quote
from thematic_analysis.prompts import join_system_prompt_sections

if TYPE_CHECKING:
    from thematic_analysis.research_context import ResearchContext


@dataclass
class ThemeAggregatorConfig(AgentConfig):
    """Configuration for the Theme Aggregator agent."""

    similarity_threshold: float = 0.75
    max_quotes_per_theme: int = 10


@dataclass
class MergedTheme:
    """A theme that may combine multiple similar themes."""

    name: str
    description: str
    original_themes: list[str]
    codes: list[str]
    quotes: list[Quote] = field(default_factory=list)
    merge_rationale: str = ""


@dataclass
class ThemeAggregationResult:
    """Result of aggregating themes from multiple coders."""

    themes: list[MergedTheme]

    def to_json(self) -> str:
        """Convert to structured JSON format."""
        return json.dumps(
            {
                "themes": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "original_themes": t.original_themes,
                        "codes": t.codes,
                        "quotes": [
                            {"quote_id": q.quote_id, "text": q.text} for q in t.quotes
                        ],
                        "merge_rationale": t.merge_rationale,
                    }
                    for t in self.themes
                ]
            },
            indent=2,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary format."""
        return {
            "themes": [
                {
                    "name": t.name,
                    "description": t.description,
                    "original_themes": t.original_themes,
                    "codes": t.codes,
                    "quotes": [
                        {"quote_id": q.quote_id, "text": q.text} for q in t.quotes
                    ],
                }
                for t in self.themes
            ]
        }


THEME_AGGREGATOR_SYSTEM_PROMPT = """\
You are an expert qualitative researcher responsible for organizing themes
from multiple analysts. Your task is to merge similar themes while preserving
distinct insights.

## Guidelines for Theme Aggregation:
1. **Identify overlap**: Find themes that capture similar or related concepts
2. **Preserve nuance**: Keep themes separate if they represent distinct insights
3. **Create unified labels**: When merging, create clear labels for merged themes
4. **Refine descriptions**: Write comprehensive descriptions for merged themes
5. **Prioritize evidence**: Keep the most relevant quotes and codes

## Decision Criteria for Merging:
- Merge if: Themes describe the same pattern from different perspectives
- Merge if: One theme is a subset or variant of another
- Keep separate if: Themes capture genuinely different phenomena
- Keep separate if: Merging would obscure important distinctions

## Output Format:
Respond with a JSON object containing:
- "merge_groups": List of theme groups to merge
- "retain_themes": List of theme names to keep separate

Example:
```json
{{
  "merge_groups": [
    {{
      "merged_name": "Social Support Networks",
      "merged_description": "Comprehensive description of the merged theme",
      "original_themes": ["Peer Support", "Family Support"],
      "rationale": "Both describe support from social connections"
    }}
  ],
  "retain_themes": ["Academic Stress", "Time Management"]
}}
```"""

THEME_AGGREGATOR_USER_PROMPT = """\
## Themes to Aggregate:
{themes_section}

## Similar Theme Groups (based on semantic similarity):
{similar_groups_section}

Please analyze these themes and determine which should be merged and which
should remain separate. Provide your response as JSON."""


THEME_AGGREGATOR_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "theme_aggregation",
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
                            "merged_name": {"type": "string"},
                            "merged_description": {"type": "string"},
                            "original_themes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "rationale": {"type": "string"},
                        },
                        "required": [
                            "merged_name",
                            "merged_description",
                            "original_themes",
                            "rationale",
                        ],
                    },
                },
                "retain_themes": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["merge_groups", "retain_themes"],
        },
    },
}


class ThemeAggregatorAgent(BaseAgent):
    """Agent that merges and organizes themes from multiple theme coders.

    The Theme Aggregator takes themes from multiple ThemeCoderAgents,
    identifies similar themes, merges them when appropriate, and
    outputs the final refined themes.
    """

    def __init__(
        self,
        config: ThemeAggregatorConfig | None = None,
        embedding_service: EmbeddingService | None = None,
        research_context: "ResearchContext | None" = None,
    ):
        """Initialize the Theme Aggregator agent.

        Args:
            config: Theme aggregator configuration.
            embedding_service: Service for computing theme similarity.
            research_context: Research context to keep aggregation
                aligned with the research question / framework.
        """
        super().__init__(config or ThemeAggregatorConfig())
        self.aggregator_config: ThemeAggregatorConfig = self.config  # type: ignore
        self.embedding_service = embedding_service or EmbeddingService()
        self.research_context = research_context

    def set_research_context(self, context: "ResearchContext") -> None:
        self.research_context = context

    def get_system_prompt(self) -> str:
        """Get the system prompt for aggregation."""
        research_section = ""
        if self.research_context and not self.research_context.is_empty():
            research_section = self.research_context.to_prompt_section(
                role="theme_aggregator"
            )
        return join_system_prompt_sections(
            THEME_AGGREGATOR_SYSTEM_PROMPT,
            research_context_instructions=research_section,
        )

    def _collect_all_themes(self, theme_results: list[ThemeResult]) -> dict[str, Theme]:
        """Collect all themes from multiple results.

        Args:
            theme_results: List of ThemeResult objects.

        Returns:
            Dict mapping theme names to Theme objects.
        """
        themes: dict[str, Theme] = {}

        for result in theme_results:
            for theme in result.themes:
                if theme.name not in themes:
                    themes[theme.name] = theme
                else:
                    # Merge quotes and codes from duplicate theme names
                    existing = themes[theme.name]
                    seen_codes = set(existing.codes)
                    for code in theme.codes:
                        if code not in seen_codes:
                            existing.codes.append(code)
                            seen_codes.add(code)

                    seen_quote_ids = {q.quote_id for q in existing.quotes}
                    for quote in theme.quotes:
                        if quote.quote_id not in seen_quote_ids:
                            existing.quotes.append(quote)
                            seen_quote_ids.add(quote.quote_id)

        return themes

    def _find_similar_groups(self, themes: dict[str, Theme]) -> list[list[str]]:
        """Group themes by semantic similarity.

        Args:
            themes: Dict mapping theme names to Theme objects.

        Returns:
            List of theme name groups that are semantically similar.
        """
        theme_names = list(themes.keys())
        if len(theme_names) <= 1:
            return [theme_names] if theme_names else []

        groups: list[list[str]] = []
        remaining = set(theme_names)

        for name in theme_names:
            if name not in remaining:
                continue

            remaining.remove(name)
            group = [name]

            for other_name in list(remaining):
                similarity = self.embedding_service.compute_similarity(name, other_name)
                if similarity >= self.aggregator_config.similarity_threshold:
                    group.append(other_name)
                    remaining.remove(other_name)

            groups.append(group)

        return groups

    def _format_themes_section(self, themes: dict[str, Theme]) -> str:
        """Format themes for the prompt."""
        lines = []
        for name, theme in themes.items():
            codes_str = ", ".join(theme.codes[:5])
            if len(theme.codes) > 5:
                codes_str += f"... (+{len(theme.codes) - 5} more)"
            lines.append(
                f"- **{name}**: {theme.description}\n"
                f"  Codes: {codes_str}\n"
                f"  Quotes: {len(theme.quotes)}"
            )
        return "\n".join(lines)

    def _format_similar_groups_section(self, groups: list[list[str]]) -> str:
        """Format similar theme groups for the prompt."""
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
        themes: dict[str, Theme],
    ) -> ThemeAggregationResult:
        """Parse the LLM response into a ThemeAggregationResult.

        Args:
            response: The raw LLM response.
            themes: Dict mapping theme names to Theme objects.

        Returns:
            ThemeAggregationResult with merged and retained themes.
        """
        data = extract_response_json(response)
        if data is None:
            # Fallback: return all themes as retained
            return self._fallback_result(themes)

        merged_themes = []

        # Process merge groups
        for group in data.get("merge_groups", []):
            merged_name = group.get("merged_name", "")
            merged_description = group.get("merged_description", "")
            original_themes = group.get("original_themes", [])
            rationale = group.get("rationale", "")

            # Collect codes and quotes from all original themes
            all_codes: list[str] = []
            all_quotes: list[Quote] = []
            seen_codes: set[str] = set()
            seen_quote_ids: set[str] = set()

            for orig_name in original_themes:
                if orig_name in themes:
                    orig_theme = themes[orig_name]
                    for code in orig_theme.codes:
                        if code not in seen_codes:
                            all_codes.append(code)
                            seen_codes.add(code)
                    for quote in orig_theme.quotes:
                        if quote.quote_id not in seen_quote_ids:
                            all_quotes.append(quote)
                            seen_quote_ids.add(quote.quote_id)

            max_quotes = self.aggregator_config.max_quotes_per_theme
            merged_themes.append(
                MergedTheme(
                    name=merged_name,
                    description=merged_description,
                    original_themes=original_themes,
                    codes=all_codes,
                    quotes=all_quotes[:max_quotes],
                    merge_rationale=rationale,
                )
            )

        # Process retained themes
        for theme_name in data.get("retain_themes", []):
            if theme_name in themes:
                theme = themes[theme_name]
                max_quotes = self.aggregator_config.max_quotes_per_theme
                merged_themes.append(
                    MergedTheme(
                        name=theme.name,
                        description=theme.description,
                        original_themes=[theme.name],
                        codes=theme.codes,
                        quotes=theme.quotes[:max_quotes],
                    )
                )

        return ThemeAggregationResult(themes=merged_themes)

    def _fallback_result(self, themes: dict[str, Theme]) -> ThemeAggregationResult:
        """Create fallback result when parsing fails.

        Args:
            themes: Dict mapping theme names to Theme objects.

        Returns:
            ThemeAggregationResult with all themes retained.
        """
        max_quotes = self.aggregator_config.max_quotes_per_theme
        merged = [
            MergedTheme(
                name=theme.name,
                description=theme.description,
                original_themes=[theme.name],
                codes=theme.codes,
                quotes=theme.quotes[:max_quotes],
            )
            for theme in themes.values()
        ]
        return ThemeAggregationResult(themes=merged)

    def aggregate(self, theme_results: list[ThemeResult]) -> ThemeAggregationResult:
        """Aggregate themes from multiple theme coder results.

        Args:
            theme_results: List of ThemeResult objects from theme coders.

        Returns:
            ThemeAggregationResult with merged and refined themes.
        """
        # Collect all themes
        themes = self._collect_all_themes(theme_results)

        if not themes:
            return ThemeAggregationResult(themes=[])

        # Find similar theme groups
        similar_groups = self._find_similar_groups(themes)

        # Build prompt
        user_prompt = THEME_AGGREGATOR_USER_PROMPT.format(
            themes_section=self._format_themes_section(themes),
            similar_groups_section=self._format_similar_groups_section(similar_groups),
        )

        # Call LLM with structured-output schema
        response = self._call_llm(
            self.get_system_prompt(),
            user_prompt,
            response_format=THEME_AGGREGATOR_RESPONSE_SCHEMA,
        )

        # Parse response
        return self._parse_response(response, themes)

    def aggregate_single(self, theme_result: ThemeResult) -> ThemeAggregationResult:
        """Aggregate themes from a single theme result.

        Args:
            theme_result: Single ThemeResult to process.

        Returns:
            ThemeAggregationResult (mostly passthrough for single result).
        """
        return self.aggregate([theme_result])
