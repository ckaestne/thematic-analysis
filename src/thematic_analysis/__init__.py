"""
Thematic analysis library.

Provides the agent, codebook, prompt, loader, and research-context primitives
used by the SQLite-backed `thematic_analysis_inc` pipeline (`ta` CLI).
"""

from thematic_analysis.loaders import (
    DataSegment,
    DocumentMetadata,
    LoadedDocument,
    documents_to_segments,
    load_directory,
    load_document,
    load_pdf,
    load_text_file,
)
from thematic_analysis.prompts import (
    AggregatorPrompts,
    CoderPrompts,
    PromptConfig,
    ReviewerPrompts,
    create_domain_prompts,
    get_prompt_config,
)
from thematic_analysis.research_context import (
    AGENT_ROLES,
    CODE_6RS,
    CONCEPTUALIZATION_GUIDANCE,
    KEYWORD_6RS,
    THEME_DEVELOPMENT_GUIDANCE,
    ResearchContext,
    create_methodology_prompt,
)


__version__ = "0.1.0"
__all__ = [
    "__version__",
    # Research context
    "ResearchContext",
    "AGENT_ROLES",
    "create_methodology_prompt",
    "KEYWORD_6RS",
    "CODE_6RS",
    "THEME_DEVELOPMENT_GUIDANCE",
    "CONCEPTUALIZATION_GUIDANCE",
    # Configurable prompts
    "PromptConfig",
    "CoderPrompts",
    "AggregatorPrompts",
    "ReviewerPrompts",
    "get_prompt_config",
    "create_domain_prompts",
    # Document loaders
    "DataSegment",
    "DocumentMetadata",
    "LoadedDocument",
    "load_pdf",
    "load_text_file",
    "load_document",
    "load_directory",
    "documents_to_segments",
]
