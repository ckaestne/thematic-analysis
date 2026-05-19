"""Agents module for thematic analysis."""

from thematic_analysis.agents.aggregator import (
    AggregationResult,
    AggregatorConfig,
    CodeAggregatorAgent,
    MergedCode,
)
from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.coder import CoderAgent, CoderConfig
from thematic_analysis.agents.reviewer import (
    ReviewDecision,
    ReviewerAgent,
    ReviewerConfig,
    ReviewResult,
)
from thematic_analysis.agents.theme_aggregator import (
    MergedTheme,
    ThemeAggregationResult,
    ThemeAggregatorAgent,
    ThemeAggregatorConfig,
)
from thematic_analysis.agents.theme_coder import (
    Theme,
    ThemeCoderAgent,
    ThemeCoderConfig,
    ThemeResult,
)


__all__ = [
    "BaseAgent",
    "AgentConfig",
    "CoderAgent",
    "CoderConfig",
    "CodeAggregatorAgent",
    "AggregatorConfig",
    "AggregationResult",
    "MergedCode",
    "ReviewerAgent",
    "ReviewerConfig",
    "ReviewResult",
    "ReviewDecision",
    "ThemeCoderAgent",
    "ThemeCoderConfig",
    "Theme",
    "ThemeResult",
    "ThemeAggregatorAgent",
    "ThemeAggregatorConfig",
    "ThemeAggregationResult",
    "MergedTheme",
]
