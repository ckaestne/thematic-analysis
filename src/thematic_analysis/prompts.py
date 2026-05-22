"""Configurable prompts for Thematic-LM agents.

This module externalizes prompts from agent code to allow:
- Custom prompt templates
- Domain-specific adaptations
- Research context injection
- Multi-language support

Based on Issue #38: Externalize prompts from agent code.
"""

from dataclasses import dataclass, field


def join_system_prompt_sections(
  general_instructions: str,
  *,
  research_context_instructions: str = "",
  identity_instructions: str = "",
) -> str:
  """Join precomputed system-prompt sections.

  The default layout is fixed role instructions first, then any
  research-context-specific guidance, then identity/persona guidance.
  This is a pure string operation at runtime; any role-specific research
  context fragment is assumed to have been generated earlier and stored on
  the ``ResearchContext`` object. Older templates that still embed
  ``{identity_section}`` are supported so custom prompts keep working.
  """
  identity = identity_instructions.strip()
  general = general_instructions.strip()
  if "{identity_section}" in general:
    general = general.format(identity_section=identity)
    identity = ""
  else:
    general = general.format(identity_section="")

  sections = [general]
  research = research_context_instructions.strip()
  if research:
    sections.append(research)
  if identity:
    sections.append(identity)
  return "\n\n".join(section for section in sections if section)


# Coder prompts. The system prompt follows the Thematic-LM paper
# (Appendix "Main Prompts", p.1106–1112): 1–3 codes per segment, each
# with a short description and one or more verbatim quotes.
#
# Previous wording — kept as a reference for the eventual A/B comparison:
#
# CODER_SYSTEM_PROMPT_OLD = """\
# You are an expert qualitative researcher performing thematic coding.
# Your task is to analyze text segments and assign meaningful codes
# that capture the key concepts, themes, and patterns.
#
# ## Guidelines for Coding:
# 1. **Read carefully**: Understand the full meaning and context of the text
# 2. **Identify key concepts**: Look for important ideas, experiences, or patterns
# 3. **Create descriptive codes**: Codes should be concise but meaningful labels
# 4. **Consider existing codes**: When possible, use or adapt existing codes
# 5. **Be consistent**: Apply codes consistently across similar content
#
# ## Code Quality Criteria (6 Rs):
# - **Reciprocal**: Codes should relate meaningfully to the data
# - **Recognizable**: Codes should be clear and understandable
# - **Responsive**: Codes should address the research questions
# - **Resourceful**: Codes should capture nuanced meanings
#
# {identity_section}
#
# ## Output Format:
# Respond with a JSON object containing:
# - "codes": List of code labels assigned to this segment
# - "rationales": List of brief explanations for each code assignment
# - "is_new": List of booleans indicating if each code is new (not in codebook)
# """

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
list of codes. Do not invent codes to cover off-topic material."""


CODER_USER_PROMPT = """\
## Current Codebook:
{codebook_section}

## Text Segment to Code:
Text: "{segment_text}"

Output codes following the required schema."""


@dataclass
class CoderPrompts:
    """Prompts for the Coder agent."""

    system_prompt: str = CODER_SYSTEM_PROMPT
    user_prompt: str = CODER_USER_PROMPT


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
class PromptConfig:
    """Complete prompt configuration for all agents."""

    coder: CoderPrompts = field(default_factory=CoderPrompts)
    aggregator: AggregatorPrompts = field(default_factory=AggregatorPrompts)
    reviewer: ReviewerPrompts = field(default_factory=ReviewerPrompts)


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

    return config
