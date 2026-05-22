"""Research context for qualitative analysis.

A research context is a single freeform statement that names the research
focus and (typically) the research question(s) driving the analysis. It is
intentionally unstructured: research questions in inductive thematic
analysis are often plural and evolve through coding, so forcing them into
fixed slots distorts the practice.

The context can optionally carry per-agent *tailored prompts* — LLM-derived
prompt sections that translate the freeform description into a brief
oriented at one specific agent's job (coder, theme coder, etc.). When
present they are used in place of the raw description; otherwise the
description is injected verbatim.
"""

from dataclasses import dataclass, field


AGENT_ROLES: tuple[str, ...] = (
    "coder",
    "coding_critic",
    "reviewer",
)


@dataclass
class ResearchContext:
    """Freeform research context, optionally with per-role tailored prompts.

    Attributes:
        description: Freeform prose describing the research context and
            research question(s). This is what the user types.
        tailored_prompts: Optional dict mapping agent role (see
            ``AGENT_ROLES``) to a tailored prompt section generated from
            ``description``. Stale on description change; cleared by the
            storage layer when the description is updated.
    """

    description: str = ""
    tailored_prompts: dict[str, str] = field(default_factory=dict)

    def to_prompt_section(self, role: str | None = None) -> str:
        """Return the prompt section to inject into an agent's system prompt.

      If a tailored prompt exists for ``role``, return the stored fragment
      as-is. These fragments are generated only when the research context is
      updated/regenerated, then reused for every downstream coding step.
      Otherwise fall back to the raw description wrapped in a generic header
      so the system stays usable before tailored prompts have been generated.
        """
        if role is not None:
            tailored = self.tailored_prompts.get(role)
            if tailored:
                return tailored.strip()
        if not self.description.strip():
            return ""
        return f"## Research Context\n{self.description.strip()}"

    def is_empty(self) -> bool:
        return not self.description.strip()


# The 6 Rs Framework for Keyword and Code Selection (Naeem et al. 2025)

KEYWORD_6RS = """
## 6 Rs Framework for Keyword Selection (Naeem et al. 2025)

When selecting keywords from the data, apply these criteria:

1. **Realness**: Keywords should represent genuine, authentic expressions from
   participants, not researcher-imposed terms

2. **Richness**: Keywords should capture the depth and nuance of meaning,
   not just surface-level descriptions

3. **Repetition**: Keywords that appear frequently across the data may
   indicate important patterns (but frequency alone is not sufficient)

4. **Rationale**: There should be a clear justification for why this
   keyword captures something meaningful about the research question

5. **Repartee**: Keywords should maintain the "voice" of participants,
   preserving the interactional quality of the data

6. **Regal**: Keywords should be elevated to represent broader conceptual
   significance while staying grounded in the data
"""

CODE_6RS = """
## 6 Rs Framework for Code Quality (Naeem et al. 2025)

When assigning and evaluating codes, ensure they meet these criteria:

1. **Reciprocal**: Codes should have a mutual, bidirectional relationship
   with the data - the code illuminates the data and the data supports the code

2. **Recognizable**: Codes should be clear and understandable to other
   researchers; avoid jargon or overly abstract labels

3. **Responsive**: Codes should directly address and relate to the research
   questions being investigated

4. **Resourceful**: Codes should be analytically productive, capturing
   nuanced meanings that contribute to deeper understanding
"""

THEME_DEVELOPMENT_GUIDANCE = """
## Theme Development Guidance (Naeem et al. 2025)

Themes should be developed by:

1. **Organizing codes**: Group related codes into categories based on
   their inter-relationships, not just surface similarity

2. **Considering theory**: Let the theoretical framework guide how you
   understand relationships between codes

3. **Looking for patterns**: Identify recurring patterns that speak to
   the research questions

4. **Building coherence**: Themes should tell a coherent story about
   the data that advances understanding

5. **Staying grounded**: While abstracting from codes to themes,
   maintain connection to the original data
"""

CONCEPTUALIZATION_GUIDANCE = """
## Conceptualization Guidance (Naeem et al. 2025)

Beyond identifying themes, work toward conceptualization:

1. **Interpret coherently**: Bring together codes and themes into a
   coherent interpretation that defines new concepts

2. **Build theory**: Move from description to explanation - what do
   the themes tell us about the phenomenon?

3. **Connect to literature**: How do emerging concepts relate to
   existing theoretical frameworks?

4. **Synthesize**: Develop a conceptual model that integrates the
   findings into a coherent framework
"""


def create_methodology_prompt(
    research_context: ResearchContext | None = None,
    include_6rs_keywords: bool = False,
    include_6rs_codes: bool = True,
    include_theme_guidance: bool = False,
    include_conceptualization: bool = False,
    role: str | None = None,
) -> str:
    """Create a methodology prompt section.

    Args:
        research_context: Optional research context to include first.
        include_6rs_keywords: Include keyword selection 6Rs.
        include_6rs_codes: Include code quality 6Rs.
        include_theme_guidance: Include theme development guidance.
        include_conceptualization: Include conceptualization guidance.
        role: Optional agent role; selects a tailored prompt when present.

    Returns:
        Formatted prompt section.
    """
    sections: list[str] = []

    if research_context is not None and not research_context.is_empty():
        section = research_context.to_prompt_section(role=role)
        if section:
            sections.append(section)

    if include_6rs_keywords:
        sections.append(KEYWORD_6RS.strip())

    if include_6rs_codes:
        sections.append(CODE_6RS.strip())

    if include_theme_guidance:
        sections.append(THEME_DEVELOPMENT_GUIDANCE.strip())

    if include_conceptualization:
        sections.append(CONCEPTUALIZATION_GUIDANCE.strip())

    return "\n\n".join(sections)
