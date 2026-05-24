"""Tests for per-task LLM env-var configuration."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from thematic_analysis.agents.aggregator import AggregatorConfig
from thematic_analysis.agents.coder import CoderConfig
from thematic_analysis.agents.reviewer import ReviewerConfig
from thematic_analysis.agents.theme_coder import ThemeCoderConfig
from thematic_analysis.llm_config import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    apply_task_env,
    ensure_llm_model_env,
    env_max_tokens,
    env_model,
    env_temperature,
    resolve_max_tokens,
    resolve_model,
    resolve_temperature,
)


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    """Clear LLM_* vars before each test so we control the env precisely."""
    for var in list(os.environ):
        if var.startswith("LLM_"):
            monkeypatch.delenv(var, raising=False)
    yield


def test_env_lookup_returns_none_when_unset():
    assert env_model("coder") is None
    assert env_temperature("coder") is None
    assert env_max_tokens("coder") is None


def test_env_lookup_reads_task_specific_vars(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_CODER", "anthropic/claude-opus-4-7")
    monkeypatch.setenv("LLM_TEMPERATURE_CODER", "0.2")
    monkeypatch.setenv("LLM_MAX_TOKENS_CODER", "2048")
    assert env_model("coder") == "anthropic/claude-opus-4-7"
    assert env_temperature("coder") == 0.2
    assert env_max_tokens("coder") == 2048


def test_resolve_falls_back_to_defaults():
    assert resolve_model("coder") == DEFAULT_MODEL
    assert resolve_temperature("coder") == DEFAULT_TEMPERATURE
    assert resolve_max_tokens("coder") == DEFAULT_MAX_TOKENS


def test_resolve_uses_global_llm_model_when_task_unset(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.4")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2222")
    assert resolve_model("coder") == "global-model"
    assert resolve_temperature("coder") == 0.4
    assert resolve_max_tokens("coder") == 2222


def test_resolve_task_specific_beats_global(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("LLM_MODEL_CODER", "task-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.4")
    monkeypatch.setenv("LLM_TEMPERATURE_CODER", "0.1")
    assert resolve_model("coder") == "task-model"
    assert resolve_temperature("coder") == 0.1
    # Aggregator still gets the global value.
    assert resolve_model("aggregator") == "global-model"


def test_resolve_explicit_fallback_used_over_default():
    assert resolve_model("segmenter", fallback="gemini/foo") == "gemini/foo"
    assert resolve_temperature("segmenter", fallback=0.0) == 0.0


def test_resolve_env_beats_explicit_fallback(monkeypatch):
    """LLM_MODEL still wins over a function-supplied fallback so users can
    override segmenter-style hardcoded defaults globally."""
    monkeypatch.setenv("LLM_MODEL", "global-model")
    assert resolve_model("segmenter", fallback="gemini/foo") == "global-model"


def test_apply_task_env_uses_global_llm_vars(monkeypatch):
    """apply_task_env (used by tailor) honours both task-specific and global
    LLM_* vars."""
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("LLM_MAX_TOKENS_TAILOR", "999")
    llm = SimpleNamespace(model="orig", temperature=0.5, max_output_tokens=1)
    apply_task_env(llm, "tailor")
    assert llm.model == "global-model"
    assert llm.temperature == 0.5  # nothing set
    assert llm.max_output_tokens == 999


def test_apply_task_env_task_specific_beats_global(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("LLM_MODEL_TAILOR", "task-model")
    llm = SimpleNamespace(model="orig", temperature=0.5, max_output_tokens=1)
    apply_task_env(llm, "tailor")
    assert llm.model == "task-model"


def test_apply_task_env_noop_when_unset():
    llm = SimpleNamespace(model="m", temperature=0.5, max_output_tokens=1)
    apply_task_env(llm, "tailor")
    assert (llm.model, llm.temperature, llm.max_output_tokens) == ("m", 0.5, 1)


def test_ensure_llm_model_env_sets_default_when_unset():
    assert os.environ.get("LLM_MODEL") is None
    ensure_llm_model_env()
    assert os.environ["LLM_MODEL"] == DEFAULT_MODEL


def test_ensure_llm_model_env_preserves_existing(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "my-model")
    ensure_llm_model_env()
    assert os.environ["LLM_MODEL"] == "my-model"


@pytest.mark.parametrize(
    "config_cls, expected_task",
    [
        (CoderConfig, "coder"),
        (AggregatorConfig, "aggregator"),
        (ReviewerConfig, "reviewer"),
        (ThemeCoderConfig, "theme_coder"),
    ],
)
def test_subclass_tasks(config_cls, expected_task):
    assert config_cls().task == expected_task


class _FakeLLM:
    """Stand-in that mimics openhands.sdk.LLM's mutable attrs."""

    def __init__(self):
        self.model = "sdk-default"
        self.temperature = -1.0
        self.max_output_tokens = -1


def _patch_load_from_env(monkeypatch):
    """Make BaseAgent.llm produce a _FakeLLM instead of touching the network."""
    import thematic_analysis.agents.base as base_mod

    fake = _FakeLLM()
    monkeypatch.setattr(base_mod.LLM, "load_from_env", staticmethod(lambda: fake))
    return fake


def test_llm_property_applies_task_env(monkeypatch):
    """Resolution goes through BaseAgent directly so we don't need to build
    a full CoderAgent/ReviewerAgent (which need a Codebook etc.)."""
    fake = _patch_load_from_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_CODER", "anthropic/claude-opus-4-7")
    monkeypatch.setenv("LLM_TEMPERATURE_CODER", "0.33")
    monkeypatch.setenv("LLM_MAX_TOKENS_CODER", "1234")

    from thematic_analysis.agents.base import BaseAgent

    class _Bare(BaseAgent):
        def get_system_prompt(self) -> str:
            return ""

    agent = _Bare(CoderConfig())
    llm = agent.llm
    assert llm is fake
    assert fake.model == "anthropic/claude-opus-4-7"
    assert fake.temperature == 0.33
    assert fake.max_output_tokens == 1234


def test_llm_property_explicit_config_beats_env(monkeypatch):
    fake = _patch_load_from_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_REVIEWER", "from-env")
    monkeypatch.setenv("LLM_TEMPERATURE_REVIEWER", "0.99")

    from thematic_analysis.agents.base import BaseAgent

    class _Bare(BaseAgent):
        def get_system_prompt(self) -> str:
            return ""

    agent = _Bare(ReviewerConfig(model="from-code", temperature=0.05))
    agent.llm  # noqa: B018 - trigger lazy load
    assert fake.model == "from-code"
    assert fake.temperature == 0.05


def test_llm_property_falls_back_to_defaults(monkeypatch):
    """When nothing is configured, the resolved DEFAULT_MODEL kicks in."""
    fake = _patch_load_from_env(monkeypatch)

    from thematic_analysis.agents.base import BaseAgent

    class _Bare(BaseAgent):
        def get_system_prompt(self) -> str:
            return ""

    _Bare(AggregatorConfig()).llm  # noqa: B018
    assert fake.model == DEFAULT_MODEL
    assert fake.temperature == DEFAULT_TEMPERATURE
    assert fake.max_output_tokens == DEFAULT_MAX_TOKENS


def test_llm_property_uses_global_llm_model(monkeypatch):
    """LLM_MODEL alone (no task-specific override) drives every agent."""
    fake = _patch_load_from_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.42")

    from thematic_analysis.agents.base import BaseAgent

    class _Bare(BaseAgent):
        def get_system_prompt(self) -> str:
            return ""

    _Bare(CoderConfig()).llm  # noqa: B018
    assert fake.model == "global-model"
    assert fake.temperature == 0.42


def test_llm_property_task_specific_beats_global(monkeypatch):
    fake = _patch_load_from_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("LLM_MODEL_CODER", "coder-model")

    from thematic_analysis.agents.base import BaseAgent

    class _Bare(BaseAgent):
        def get_system_prompt(self) -> str:
            return ""

    _Bare(CoderConfig()).llm  # noqa: B018
    assert fake.model == "coder-model"


def test_theme_coder_env(monkeypatch):
    fake = _patch_load_from_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_THEME_CODER", "anthropic/claude-sonnet-4-6")

    from thematic_analysis.agents.base import BaseAgent

    class _Bare(BaseAgent):
        def get_system_prompt(self) -> str:
            return ""

    _Bare(ThemeCoderConfig()).llm  # noqa: B018
    assert fake.model == "anthropic/claude-sonnet-4-6"
