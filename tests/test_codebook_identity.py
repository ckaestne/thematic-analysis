"""Tests for codebook object identity (regression tests for falsy codebook bug)."""

from thematic_analysis.agents import (
    CodeAggregatorAgent,
    CoderAgent,
    ReviewerAgent,
    ThemeCoderAgent,
)
from thematic_analysis.codebook import Codebook


class TestCodebookIdentityBug:
    """
    Regression tests for the falsy codebook bug.

    The bug: When an empty Codebook was passed to an agent, the expression
    `self.codebook = codebook or Codebook()` would create a NEW Codebook
    because empty codebooks are falsy (len=0, so bool=False).

    This caused the pipeline's codebook to not be updated by the reviewer.
    """

    def test_coder_agent_preserves_codebook_identity(self):
        """CoderAgent should use the exact codebook passed to it."""
        codebook = Codebook(use_mock_embeddings=True)
        original_id = id(codebook)

        agent = CoderAgent(codebook=codebook)

        assert id(agent.codebook) == original_id
        assert agent.codebook is codebook

    def test_reviewer_agent_preserves_codebook_identity(self):
        """ReviewerAgent should use the exact codebook passed to it."""
        codebook = Codebook(use_mock_embeddings=True)
        original_id = id(codebook)

        agent = ReviewerAgent(codebook=codebook)

        assert id(agent.codebook) == original_id
        assert agent.codebook is codebook

    def test_aggregator_agent_preserves_codebook_identity(self):
        """CodeAggregatorAgent should use the exact codebook passed to it."""
        codebook = Codebook(use_mock_embeddings=True)
        original_id = id(codebook)

        agent = CodeAggregatorAgent(codebook=codebook)

        assert id(agent.codebook) == original_id
        assert agent.codebook is codebook

    def test_theme_coder_agent_preserves_codebook_identity(self):
        """ThemeCoderAgent should use the exact codebook passed to it."""
        codebook = Codebook(use_mock_embeddings=True)
        original_id = id(codebook)

        agent = ThemeCoderAgent(codebook=codebook)

        assert id(agent.codebook) == original_id
        assert agent.codebook is codebook

    def test_empty_codebook_is_falsy(self):
        """Verify that empty codebooks are indeed falsy (the root cause)."""
        codebook = Codebook(use_mock_embeddings=True)
        assert len(codebook) == 0
        assert bool(codebook) is False
