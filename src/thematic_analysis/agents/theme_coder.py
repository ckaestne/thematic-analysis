"""Theme Coder agent for developing themes from the codebook."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.codebook import Codebook, Quote
from thematic_analysis.prompts import join_system_prompt_sections


if TYPE_CHECKING:
    from thematic_analysis.research_context import ResearchContext


@dataclass
class ThemeCoderConfig(AgentConfig):
    """Configuration for the Theme Coder agent."""

    max_themes: int = 10
    max_quotes_per_theme: int = 10
    min_codes_per_theme: int = 2  # Minimum codes to form a theme
    include_theory_guidance: bool = True  # Include theory-aligned theme development


@dataclass
class Theme:
    """A theme derived from codes in the codebook."""

    name: str
    description: str
    codes: list[str]
    quotes: list[Quote] = field(default_factory=list)


@dataclass
class ThemeResult:
    """Result of theme development."""

    themes: list[Theme]

    def to_json(self) -> str:
        """Convert to structured JSON format."""
        return json.dumps(
            {
                "themes": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "codes": t.codes,
                        "quotes": [
                            {"quote_id": q.quote_id, "text": q.text} for q in t.quotes
                        ],
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
                    "codes": t.codes,
                    "quotes": [
                        {"quote_id": q.quote_id, "text": q.text} for q in t.quotes
                    ],
                }
                for t in self.themes
            ]
        }


THEME_CODER_SYSTEM_PROMPT = """\
You are an expert qualitative researcher performing thematic analysis.
Your task is to identify overarching themes from a codebook of codes and quotes.

## Theme Development Guidelines:
1. **Look for patterns**: Identify recurring ideas, concepts, or experiences
2. **Think abstractly**: Themes should capture deeper meanings beyond individual codes
3. **Stay grounded**: Themes must be supported by the codes and quotes in the data
4. **Be comprehensive**: Themes should together capture the key insights from the data
5. **Avoid overlap**: Each theme should represent a distinct aspect of the data

## Theme Quality Criteria:
- **Coherent**: All codes within a theme should relate meaningfully
- **Distinctive**: Themes should be clearly distinguishable from each other
- **Data-driven**: Themes should emerge from and be supported by the codes
- **Insightful**: Themes should reveal patterns not obvious from codes alone

## Output Format:
Respond with a JSON object containing a list of themes:
```json
{{
  "themes": [
    {{
      "name": "Theme name",
      "description": "Brief description of what this theme captures",
      "codes": ["code1", "code2", "code3"]
    }}
  ]
}}
```"""

THEME_CODER_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "theme_development",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "themes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "codes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["name", "description", "codes"],
                    },
                },
            },
            "required": ["themes"],
        },
    },
}


THEME_CODER_USER_PROMPT = """\
## Codebook Summary:
Total codes: {total_codes}

## Codes and Sample Quotes:
{codes_section}

Please analyze these codes and identify overarching themes that capture
the key patterns and insights in the data. Provide your response as JSON."""


class ThemeCoderAgent(BaseAgent):
    """Agent that develops themes from the finalized codebook.

    The Theme Coder analyzes codes and their associated quotes holistically
    to identify overarching themes that reflect deeper insights into the data.

    Supports theory-aligned theme development with research context (Naeem et al. 2025).
    """

    def __init__(
        self,
        config: ThemeCoderConfig | None = None,
        codebook: Codebook | None = None,
        research_context: ResearchContext | None = None,
    ):
        """Initialize the Theme Coder agent.

        Args:
            config: Theme coder configuration.
            codebook: The codebook to analyze.
            research_context: Research context for theory-aligned themes.
        """
        super().__init__(config or ThemeCoderConfig())
        self.theme_config: ThemeCoderConfig = self.config  # type: ignore
        self.codebook = codebook if codebook is not None else Codebook()
        self.research_context = research_context

    def set_research_context(self, context: ResearchContext) -> None:
        """Set or update the research context.

        Args:
            context: The research context to use for theme development.
        """
        self.research_context = context

    def get_system_prompt(self) -> str:
        """Get the system prompt with optional identity and research context."""
        identity_section = ""
        if self.config.identity:
            identity_section = f"""
## Your Perspective:
You are analyzing from the following perspective: {self.config.identity}
Let this perspective inform how you interpret patterns and develop themes,
while staying grounded in the data."""

        # Add research context for theory-aligned theme development
        research_section = ""
        if self.research_context and not self.research_context.is_empty():
            research_section = self.research_context.to_prompt_section(
                role="theme_coder"
            )
        return join_system_prompt_sections(
            THEME_CODER_SYSTEM_PROMPT,
            research_context_instructions=research_section,
            identity_instructions=identity_section,
        )

    def _format_codes_section(self) -> str:
        """Format codes and quotes for the prompt."""
        lines = []
        for entry in self.codebook.entries:
            quote_samples = entry.quotes[:3]
            quote_text = ""
            if quote_samples:
                quote_text = " | ".join(f'"{q.text[:80]}..."' for q in quote_samples)
            num_quotes = len(entry.quotes)
            lines.append(f"- **{entry.code}** ({num_quotes} quotes): {quote_text}")
        return "\n".join(lines)

    def _compress_codebook(self) -> str:
        """Compress the codebook for token efficiency.

        This is a simple compression that only includes code names.
        For production use, consider LLMLingua integration.
        """
        return ", ".join(entry.code for entry in self.codebook.entries)

    def _parse_response(self, response: str) -> list[Theme]:
        """Parse the LLM response into themes.

        Args:
            response: The raw LLM response.

        Returns:
            List of Theme objects.
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
                return []

        try:
            data = json.loads(json_str)
            themes = []

            for theme_data in data.get("themes", [])[: self.theme_config.max_themes]:
                name = theme_data.get("name", "")
                description = theme_data.get("description", "")
                codes = theme_data.get("codes", [])

                # Skip themes with too few codes
                if len(codes) < self.theme_config.min_codes_per_theme:
                    continue

                # Collect quotes from the specified codes
                quotes = self._collect_quotes_for_codes(codes)

                themes.append(
                    Theme(
                        name=name,
                        description=description,
                        codes=codes,
                        quotes=quotes[: self.theme_config.max_quotes_per_theme],
                    )
                )

            return themes

        except json.JSONDecodeError:
            return []

    def _collect_quotes_for_codes(self, codes: list[str]) -> list[Quote]:
        """Collect quotes from the codebook for specified codes.

        Args:
            codes: List of code names.

        Returns:
            List of quotes from those codes.
        """
        quotes = []
        seen_ids = set()

        # Create a lookup for faster access
        code_lookup = {entry.code: entry for entry in self.codebook.entries}

        for code in codes:
            if code in code_lookup:
                for quote in code_lookup[code].quotes:
                    if quote.quote_id not in seen_ids:
                        quotes.append(quote)
                        seen_ids.add(quote.quote_id)

        return quotes

    def develop_themes(self) -> ThemeResult:
        """Develop themes from the codebook.

        Returns:
            ThemeResult with identified themes.
        """
        if len(self.codebook) == 0:
            return ThemeResult(themes=[])

        user_prompt = THEME_CODER_USER_PROMPT.format(
            total_codes=len(self.codebook),
            codes_section=self._format_codes_section(),
        )

        response = self._call_llm(
            self.get_system_prompt(),
            user_prompt,
            response_format=THEME_CODER_RESPONSE_SCHEMA,
        )
        themes = self._parse_response(response)

        return ThemeResult(themes=themes)

    def develop_themes_from_codebook(self, codebook: Codebook) -> ThemeResult:
        """Develop themes from a provided codebook.

        Args:
            codebook: The codebook to analyze.

        Returns:
            ThemeResult with identified themes.
        """
        self.codebook = codebook
        return self.develop_themes()
