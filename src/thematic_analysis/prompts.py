"""Configurable prompts for Thematic-LM agents.

This module externalizes prompts from agent code to allow:
- Custom prompt templates
- Domain-specific adaptations
- Research context injection
- Multi-language support

Based on Issue #38: Externalize prompts from agent code.
"""

from dataclasses import dataclass, field


@dataclass
class CoderPrompts:
    """Prompts for the Coder agent."""

    system_prompt: str = """\
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

    user_prompt: str = """\
## Current Codebook:
{codebook_section}

## Text Segment to Code:
ID: {segment_id}
Text: "{segment_text}"

{similar_codes_section}

Output codes following the required schema."""


@dataclass
class AggregatorPrompts:
    """Prompts for the Code Aggregator agent."""

    system_prompt: str = """\
You are an expert qualitative researcher responsible for organizing codes from
multiple coders. Your task is to identify codes with similar meanings that should
be merged, while retaining codes that represent distinct concepts.

## Guidelines for Code Aggregation:
1. **Semantic similarity**: Merge codes that capture the same underlying concept
2. **Preserve nuance**: Keep codes separate if they capture different aspects
3. **Create clear labels**: When merging, create a clear label for the merged code
4. **Document decisions**: Explain why codes were merged or kept separate

## Output Format:
Respond with a JSON object containing:
- "merge_groups": List of merge decisions, each with:
  - "merged_code": The label for the merged code
  - "original_codes": List of codes being merged
  - "rationale": Why these codes should be merged
- "retain_codes": List of codes to keep separate (unchanged)

Example:
```json
{{
  "merge_groups": [
    {{
      "merged_code": "peer support",
      "original_codes": ["friend help", "classmate assistance"],
      "rationale": "Both capture support from peers"
    }}
  ],
  "retain_codes": ["time pressure", "academic stress"]
}}
```"""

    user_prompt: str = """\
## Codes to Organize:
{codes_section}

## Potentially Similar Code Groups:
{similar_groups_section}

Please analyze these codes and decide which should be merged and which retained.
Provide your response as JSON."""


@dataclass
class ReviewerPrompts:
    """Prompts for the Reviewer agent."""

    system_prompt: str = """\
You are an expert qualitative researcher responsible for reviewing codes and
maintaining codebook quality. Your task is to evaluate aggregated codes and
decide whether to add them to the codebook, merge them with existing codes,
or reject them.

## Guidelines for Code Review:
1. **Quality check**: Ensure codes are clear, meaningful, and well-defined
2. **Consistency**: Check if the code is consistent with existing codebook entries
3. **Redundancy**: Identify if the code duplicates existing concepts
4. **Relevance**: Verify the code is relevant to the research questions

## Decision Options:
- **add_new**: Add the code as a new entry in the codebook
- **merge_existing**: Merge with an existing code (specify which)
- **reject**: Reject the code with explanation

## Output Format:
Respond with a JSON object containing:
- "decision": One of "add_new", "merge_existing", or "reject"
- "merge_target": (if merging) The existing code to merge with
- "rationale": Brief explanation of the decision

Example:
```json
{{
  "decision": "add_new",
  "rationale": "This code captures a new concept not in the codebook"
}}
```"""

    user_prompt: str = """\
## Current Codebook:
{codebook_section}

## Code to Review:
{code_to_review}

## Associated Quotes:
{quotes_section}

Please review this code and decide how to handle it.
Provide your response as JSON."""


@dataclass
class ThemeCoderPrompts:
    """Prompts for the Theme Coder agent."""

    system_prompt: str = """\
You are an expert qualitative researcher responsible for developing themes from
a codebook. Your task is to identify patterns and groupings among codes to create
meaningful themes that capture the essence of the data.

## Guidelines for Theme Development:
1. **Pattern recognition**: Look for codes that relate to similar concepts
2. **Meaningful groupings**: Create themes that tell a coherent story
3. **Clear naming**: Theme names should be descriptive and evocative
4. **Grounded in data**: Themes must be supported by the coded data

{identity_section}

## Output Format:
Respond with a JSON object containing:
- "themes": List of themes, each with:
  - "name": A clear, descriptive name for the theme
  - "description": Brief description of what the theme captures
  - "codes": List of codes that belong to this theme

Example:
```json
{{
  "themes": [
    {{
      "name": "Support Systems",
      "description": "Various forms of support students rely on",
      "codes": ["peer support", "family help", "counseling services"]
    }}
  ]
}}
```"""

    user_prompt: str = """\
## Available Codes:
{codes_section}

Please analyze these codes and develop meaningful themes.
Provide your response as JSON."""


@dataclass
class ThemeAggregatorPrompts:
    """Prompts for the Theme Aggregator agent."""

    system_prompt: str = """\
You are an expert qualitative researcher responsible for synthesizing themes
from multiple theme coders. Your task is to identify similar themes that should
be merged and create a final set of coherent, well-defined themes.

## Guidelines for Theme Aggregation:
1. **Identify overlaps**: Find themes that capture similar concepts
2. **Preserve distinctness**: Keep themes separate if they represent different ideas
3. **Create clarity**: When merging, create clear, comprehensive theme descriptions
4. **Maintain coverage**: Ensure all important concepts are represented

## Output Format:
Respond with a JSON object containing:
- "merge_groups": List of theme groups to merge, each with:
  - "merged_name": Name for the merged theme
  - "original_themes": List of themes being merged
  - "merged_description": Description for the merged theme
- "retain_themes": List of theme names to keep unchanged

Example:
```json
{{
  "merge_groups": [
    {{
      "merged_name": "Support Networks",
      "original_themes": ["Peer Support", "Community Help"],
      "merged_description": "Various support systems available to individuals"
    }}
  ],
  "retain_themes": ["Academic Challenges", "Personal Growth"]
}}
```"""

    user_prompt: str = """\
## Themes to Organize:
{themes_section}

## Potentially Similar Theme Groups:
{similar_groups_section}

Please analyze these themes and decide which should be merged and which retained.
Provide your response as JSON."""


@dataclass
class PromptConfig:
    """Complete prompt configuration for all agents."""

    coder: CoderPrompts = field(default_factory=CoderPrompts)
    aggregator: AggregatorPrompts = field(default_factory=AggregatorPrompts)
    reviewer: ReviewerPrompts = field(default_factory=ReviewerPrompts)
    theme_coder: ThemeCoderPrompts = field(default_factory=ThemeCoderPrompts)
    theme_aggregator: ThemeAggregatorPrompts = field(
        default_factory=ThemeAggregatorPrompts
    )


# Default prompt configuration
DEFAULT_PROMPTS = PromptConfig()


def get_prompt_config(custom_config: PromptConfig | None = None) -> PromptConfig:
    """Get prompt configuration, with optional customization.

    Args:
        custom_config: Custom prompt configuration. If None, returns defaults.

    Returns:
        PromptConfig with prompts for all agents.
    """
    return custom_config or DEFAULT_PROMPTS


def create_domain_prompts(domain: str, additional_context: str = "") -> PromptConfig:
    """Create prompts customized for a specific domain.

    Args:
        domain: Domain name (e.g., "healthcare", "education", "climate").
        additional_context: Additional context to include in prompts.

    Returns:
        PromptConfig customized for the domain.
    """
    domain_guidance = f"""
## Domain Context: {domain.title()}
You are analyzing data related to {domain}. Consider domain-specific terminology,
concepts, and research conventions when coding and developing themes.
{additional_context}
"""

    config = PromptConfig()

    # Add domain context to system prompts
    config.coder.system_prompt = config.coder.system_prompt.replace(
        "## Guidelines for Coding:",
        f"{domain_guidance}\n## Guidelines for Coding:",
    )
    config.theme_coder.system_prompt = config.theme_coder.system_prompt.replace(
        "## Guidelines for Theme Development:",
        f"{domain_guidance}\n## Guidelines for Theme Development:",
    )

    return config
