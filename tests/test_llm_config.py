"""Tests for per-task LLM env-var configuration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from thematic_analysis.agents.aggregator import AggregatorConfig
from thematic_analysis.agents.coder import CoderConfig
from thematic_analysis.agents.reviewer import ReviewerConfig
from thematic_analysis.agents.theme_coder import ThemeCoderConfig
from thematic_analysis.llm_config import (
    apply_task_env,
    env_max_tokens,
    env_model,
    env_temperature,
)


def test_env_lookup_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_CODER", raising=False)
    monkeypatch.delenv("LLM_TEMPERATURE_CODER", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS_CODER", raising=False)
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


def test_apply_task_env_overrides_llm(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_TAILOR", "anthropic/claude-haiku-4-5")
    monkeypatch.setenv("LLM_TEMPERATURE_TAILOR", "0.9")
    llm = SimpleNamespace(model="old", temperature=0.1, max_output_tokens=100)
    apply_task_env(llm, "tailor")
    assert llm.model == "anthropic/claude-haiku-4-5"
    assert llm.temperature == 0.9
    assert llm.max_output_tokens == 100  # untouched (no env var)


def test_apply_task_env_noop_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_TAILOR", raising=False)
    monkeypatch.delenv("LLM_TEMPERATURE_TAILOR", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS_TAILOR", raising=False)
    llm = SimpleNamespace(model="m", temperature=0.5, max_output_tokens=1)
    apply_task_env(llm, "tailor")
    assert (llm.model, llm.temperature, llm.max_output_tokens) == ("m", 0.5, 1)


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
    fake = _patch_load_from_env(monkeypatch)
    for v in (
        "LLM_MODEL_AGGREGATOR",
        "LLM_TEMPERATURE_AGGREGATOR",
        "LLM_MAX_TOKENS_AGGREGATOR",
    ):
        monkeypatch.delenv(v, raising=False)

    from thematic_analysis.agents.base import BaseAgent

    class _Bare(BaseAgent):
        def get_system_prompt(self) -> str:
            return ""

    _Bare(AggregatorConfig()).llm  # noqa: B018
    assert fake.model == "sdk-default"  # left as SDK set it
    assert fake.temperature == 0.7
    assert fake.max_output_tokens == 4096


def test_theme_coder_env(monkeypatch):
    fake = _patch_load_from_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_THEME_CODER", "anthropic/claude-sonnet-4-6")

    from thematic_analysis.agents.base import BaseAgent

    class _Bare(BaseAgent):
        def get_system_prompt(self) -> str:
            return ""

    _Bare(ThemeCoderConfig()).llm  # noqa: B018
    assert fake.model == "anthropic/claude-sonnet-4-6"
