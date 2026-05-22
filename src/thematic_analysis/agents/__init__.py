"""Agents module for thematic analysis."""

from thematic_analysis.agents.aggregator import (
    AggregatorConfig,
    CodeAggregatorAgent,
)
from thematic_analysis.agents.base import AgentConfig, BaseAgent
from thematic_analysis.agents.coder import CoderAgent, CoderConfig
from thematic_analysis.agents.reviewer import (
    ReviewerAgent,
    ReviewerConfig,
)


__all__ = [
    "BaseAgent",
    "AgentConfig",
    "CoderAgent",
    "CoderConfig",
    "CodeAggregatorAgent",
    "AggregatorConfig",
    "ReviewerAgent",
    "ReviewerConfig",
]
