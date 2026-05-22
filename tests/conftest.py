"""Pytest configuration and fixtures for thematic-lm tests."""

import pytest

from thematic_analysis.codebook import Codebook, EmbeddingService


def pytest_addoption(parser):
    """Add custom command line options."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that require real models",
    )


def pytest_configure(config):
    """Configure custom markers."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (require --run-integration)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless --run-integration is passed."""
    if config.getoption("--run-integration"):
        return

    skip_integration = pytest.mark.skip(
        reason="Integration test - use --run-integration to run"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def mock_embedding_service() -> EmbeddingService:
    """Create a mock embedding service for fast testing."""
    return EmbeddingService(use_mock=True)


@pytest.fixture
def mock_codebook(mock_embedding_service: EmbeddingService) -> Codebook:
    """Create a codebook with mock embeddings."""
    return Codebook(embedding_service=mock_embedding_service)


@pytest.fixture
def real_embedding_service() -> EmbeddingService:
    """Create a real embedding service (slow, for integration tests)."""
    return EmbeddingService(use_mock=False)


@pytest.fixture
def real_codebook(real_embedding_service: EmbeddingService) -> Codebook:
    """Create a codebook with real embeddings."""
    return Codebook(embedding_service=real_embedding_service)
