"""Per-task LLM configuration via environment variables.

Each LLM-using task (coder, aggregator, reviewer, theme_coder, segmenter,
tailor) reads task-specific env vars so a different model / temperature /
max_tokens can be wired in without code changes:

    LLM_MODEL_<TASK>          # e.g. LLM_MODEL_CODER=anthropic/claude-opus-4-5
    LLM_TEMPERATURE_<TASK>
    LLM_MAX_TOKENS_<TASK>

The global ``LLM_MODEL`` env var (consumed by ``LLM.load_from_env()``) still
acts as the underlying fallback when no task-specific override is set.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openhands.sdk import LLM


def _env(task: str, key: str) -> str | None:
    return os.environ.get(f"LLM_{key}_{task.upper()}")


def env_model(task: str) -> str | None:
    return _env(task, "MODEL")


def env_temperature(task: str) -> float | None:
    raw = _env(task, "TEMPERATURE")
    return float(raw) if raw is not None else None


def env_max_tokens(task: str) -> int | None:
    raw = _env(task, "MAX_TOKENS")
    return int(raw) if raw is not None else None


def apply_task_env(llm: "LLM", task: str) -> "LLM":
    """Override an LLM instance's model/temperature/max_output_tokens
    from task-specific env vars, if those vars are set.
    """
    model = env_model(task)
    if model:
        llm.model = model
    temperature = env_temperature(task)
    if temperature is not None:
        llm.temperature = temperature
    max_tokens = env_max_tokens(task)
    if max_tokens is not None:
        llm.max_output_tokens = max_tokens
    return llm
