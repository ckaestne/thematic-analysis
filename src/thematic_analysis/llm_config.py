"""Per-task LLM configuration via environment variables.

Resolution order (first match wins) for any task ``T``:

  explicit code/CLI value > ``LLM_MODEL_<T>`` > ``LLM_MODEL`` > builtin default

Same chain applies to ``LLM_TEMPERATURE_<T>`` / ``LLM_TEMPERATURE`` and
``LLM_MAX_TOKENS_<T>`` / ``LLM_MAX_TOKENS``. All env vars are optional;
sensible defaults kick in when nothing is set.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openhands.sdk import LLM


# Sensible global defaults applied only when no env var / explicit value
# is provided. Override globally via ``LLM_MODEL`` / ``LLM_TEMPERATURE`` /
# ``LLM_MAX_TOKENS`` or per-task via the ``_<TASK>`` suffixed variants.
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 4096


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


def resolve_model(task: str, fallback: str | None = None) -> str:
    """Resolve the effective model name for ``task``.

    Chain: ``LLM_MODEL_<TASK>`` > ``LLM_MODEL`` > ``fallback`` >
    :data:`DEFAULT_MODEL`. Always returns a non-empty string.
    """
    return (
        env_model(task)
        or os.environ.get("LLM_MODEL")
        or fallback
        or DEFAULT_MODEL
    )


def resolve_temperature(task: str, fallback: float | None = None) -> float:
    raw = _env(task, "TEMPERATURE") or os.environ.get("LLM_TEMPERATURE")
    if raw is not None:
        return float(raw)
    return fallback if fallback is not None else DEFAULT_TEMPERATURE


def resolve_max_tokens(task: str, fallback: int | None = None) -> int:
    raw = _env(task, "MAX_TOKENS") or os.environ.get("LLM_MAX_TOKENS")
    if raw is not None:
        return int(raw)
    return fallback if fallback is not None else DEFAULT_MAX_TOKENS


# Provider prefix (as used by litellm model strings) → env vars that
# litellm itself will pick up when ``LLM_API_KEY`` is not set. Empty
# list means no key needed (e.g. local Ollama).
PROVIDER_API_KEY_ENV: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "azure": ["AZURE_API_KEY", "AZURE_OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "vertex_ai": ["GOOGLE_APPLICATION_CREDENTIALS"],
    "bedrock": ["AWS_ACCESS_KEY_ID"],
    "groq": ["GROQ_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "cohere": ["COHERE_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "together_ai": ["TOGETHER_API_KEY", "TOGETHERAI_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "xai": ["XAI_API_KEY"],
    "perplexity": ["PERPLEXITYAI_API_KEY"],
    "fireworks_ai": ["FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"],
    "ollama": [],
    "ollama_chat": [],
}


def _provider_from_model(model: str) -> str | None:
    if "/" in model:
        return model.split("/", 1)[0].lower()
    return None


def llm_configured() -> tuple[bool, str | None]:
    """Cheap pre-flight: does the environment have what the agents need
    to call an LLM? Returns ``(ok, reason)`` — ``reason`` is a short
    human-readable hint when ``ok`` is False, suitable for showing in
    the web UI.

    Accepts either the generic ``LLM_API_KEY`` (read by the OpenHands
    SDK) or a provider-specific env var that litellm itself picks up
    (e.g. ``ANTHROPIC_API_KEY``, ``GEMINI_API_KEY``,
    ``OPENAI_API_KEY``). The set of provider keys checked is based on
    the configured model's prefix.
    """
    if os.environ.get("LLM_API_KEY"):
        return True, None

    model = resolve_model("default")
    provider = _provider_from_model(model)
    if provider is None:
        return (
            False,
            "No API key found: set LLM_API_KEY or a provider-specific key "
            "(e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY).",
        )

    candidates = PROVIDER_API_KEY_ENV.get(provider)
    if candidates is None:
        # Unknown provider — be permissive: any *_API_KEY in env passes.
        if any(k.endswith("_API_KEY") and v for k, v in os.environ.items()):
            return True, None
        return (
            False,
            f"No API key found for provider '{provider}'. "
            f"Set LLM_API_KEY or the provider's API key env var.",
        )

    if not candidates:
        return True, None  # local provider, no key required

    if any(os.environ.get(k) for k in candidates):
        return True, None

    names = " or ".join(candidates)
    return False, f"No API key found: set LLM_API_KEY or {names}."


def ensure_llm_model_env() -> None:
    """Ensure ``LLM_MODEL`` is set so ``LLM.load_from_env()`` validation
    doesn't fail when the user hasn't configured one. Idempotent.
    """
    os.environ.setdefault("LLM_MODEL", DEFAULT_MODEL)


def apply_task_env(llm: "LLM", task: str) -> "LLM":
    """Override an LLM instance's model/temperature/max_output_tokens
    from task-specific env vars (or the global ``LLM_*`` fallbacks).

    Only sets fields when an env var actually provides a value, so any
    SDK-supplied defaults survive when nothing is configured.
    """
    model = env_model(task) or os.environ.get("LLM_MODEL")
    if model:
        llm.model = model
    temp_raw = _env(task, "TEMPERATURE") or os.environ.get("LLM_TEMPERATURE")
    if temp_raw is not None:
        llm.temperature = float(temp_raw)
    mt_raw = _env(task, "MAX_TOKENS") or os.environ.get("LLM_MAX_TOKENS")
    if mt_raw is not None:
        llm.max_output_tokens = int(mt_raw)
    return llm

