"""Base agent class for all thematic analysis agents."""

import asyncio
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from openhands.sdk import LLM, Message, TextContent

from thematic_analysis.llm_config import (
    ensure_llm_model_env,
    resolve_max_tokens,
    resolve_model,
    resolve_temperature,
)

# Retry config for transient LLM errors (rate limits, 5xx, connection blips).
# Tunable via env so ops can crank it up without code changes.
_RETRY_MAX_ATTEMPTS = int(os.environ.get("LLM_RETRY_MAX_ATTEMPTS", "6"))
_RETRY_BASE_DELAY = float(os.environ.get("LLM_RETRY_BASE_DELAY", "2.0"))
_RETRY_MAX_DELAY = float(os.environ.get("LLM_RETRY_MAX_DELAY", "60.0"))


def _is_retryable(exc: BaseException) -> bool:
    """Match transient litellm errors without a hard import dependency."""
    name = type(exc).__name__
    if name in {
        "RateLimitError",
        "APIConnectionError",
        "InternalServerError",
        "ServiceUnavailableError",
        "BadGatewayError",
        "Timeout",
        "APITimeoutError",
    }:
        return True
    status = getattr(exc, "status_code", None)
    return status in {408, 429, 500, 502, 503, 504}


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Pull a Retry-After hint from the exception, if the provider gave one."""
    for attr in ("retry_after", "response"):
        val = getattr(exc, attr, None)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            return float(val)
        headers = getattr(val, "headers", None)
        if headers is None:
            continue
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset"):
            raw = headers.get(key) if hasattr(headers, "get") else None
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return None


def _backoff_delay(attempt: int, exc: BaseException) -> float:
    hinted = _retry_after_seconds(exc)
    if hinted is not None:
        return min(hinted, _RETRY_MAX_DELAY)
    return min(_RETRY_BASE_DELAY * (2 ** attempt), _RETRY_MAX_DELAY) * (
        0.5 + random.random()
    )


@dataclass
class AgentConfig:
    """Configuration for an agent.

    Model / temperature / max_tokens default to ``None`` so that
    task-specific env vars (``LLM_MODEL_<TASK>`` etc., see
    ``thematic_analysis.llm_config``) can take effect. Explicit values
    passed in code always win over env vars.
    """

    # Task name used to look up env-var overrides like LLM_MODEL_<TASK>.
    # Subclasses set this (e.g. "coder", "reviewer", "theme_coder").
    task: str = "default"
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    identity: str | None = None  # Optional identity/persona for the agent
    api_key: str | None = None  # API key (falls back to LLM_API_KEY env var)
    base_url: str | None = None  # Optional base URL for API


class BaseAgent(ABC):
    """Base class for all thematic analysis agents.

    Provides common functionality for LLM-based agents including
    client management and message handling. Supports both sync and async calls.
    """

    def __init__(self, config: AgentConfig | None = None):
        """Initialize the agent.

        Args:
            config: Agent configuration. Uses defaults if not provided.
        """
        self.config = config or AgentConfig()
        self._llm: LLM | None = None

    @property
    def llm(self) -> LLM:
        """Lazy load the LLM instance.

        Resolution order for model / temperature / max_tokens:
        explicit config value > ``LLM_<KEY>_<TASK>`` env > ``LLM_<KEY>``
        env > builtin default (see ``thematic_analysis.llm_config``).
        """
        if self._llm is None:
            # Make sure SDK validation doesn't fail when LLM_MODEL is unset.
            ensure_llm_model_env()
            self._llm = LLM.load_from_env()
            task = self.config.task
            self._llm.model = self.config.model or resolve_model(task)
            self._llm.temperature = (
                self.config.temperature
                if self.config.temperature is not None
                else resolve_temperature(task)
            )
            self._llm.max_output_tokens = (
                self.config.max_tokens
                if self.config.max_tokens is not None
                else resolve_max_tokens(task)
            )
        return self._llm

    def _create_messages(self, system_prompt: str, user_prompt: str) -> list[Message]:
        """Create message list for LLM call.

        Args:
            system_prompt: The system prompt with instructions.
            user_prompt: The user prompt with the task.

        Returns:
            List of Message objects.
        """
        return [
            Message(role="system", content=[TextContent(text=system_prompt)]),
            Message(role="user", content=[TextContent(text=user_prompt)]),
        ]

    def _extract_text(self, response) -> str:
        """Extract text content from LLM response.

        Args:
            response: The LLM response object.

        Returns:
            Extracted text content.
        """
        content_parts = []
        for part in response.message.content:
            if isinstance(part, TextContent):
                content_parts.append(part.text)
        return "".join(content_parts)

    def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: dict | None = None,
    ) -> str:
        """Call the LLM with the given prompts (synchronous).

        Args:
            system_prompt: The system prompt with instructions.
            user_prompt: The user prompt with the task.
            response_format: Optional litellm response_format spec to constrain
                the model's output (e.g. ``{"type": "json_schema", "json_schema": ...}``).

        Returns:
            The LLM response text.
        """
        messages = self._create_messages(system_prompt, user_prompt)
        kwargs = {"response_format": response_format} if response_format else {}
        response = self._completion_with_retry(messages, kwargs)
        return self._extract_text(response)

    def _completion_with_retry(self, messages, kwargs):
        last: BaseException | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                return self.llm.completion(messages=messages, **kwargs)
            except Exception as exc:
                if not _is_retryable(exc) or attempt == _RETRY_MAX_ATTEMPTS - 1:
                    raise
                last = exc
                time.sleep(_backoff_delay(attempt, exc))
        assert last is not None
        raise last

    async def _completion_with_retry_async(self, messages, kwargs):
        loop = asyncio.get_event_loop()
        last: BaseException | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                return await loop.run_in_executor(
                    None, lambda: self.llm.completion(messages=messages, **kwargs)
                )
            except Exception as exc:
                if not _is_retryable(exc) or attempt == _RETRY_MAX_ATTEMPTS - 1:
                    raise
                last = exc
                await asyncio.sleep(_backoff_delay(attempt, exc))
        assert last is not None
        raise last

    async def _call_llm_async(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: dict | None = None,
    ) -> str:
        """Call the LLM with the given prompts (asynchronous).

        Args:
            system_prompt: The system prompt with instructions.
            user_prompt: The user prompt with the task.
            response_format: Optional litellm response_format spec to constrain
                the model's output.

        Returns:
            The LLM response text.
        """
        messages = self._create_messages(system_prompt, user_prompt)
        kwargs = {"response_format": response_format} if response_format else {}
        response = await self._completion_with_retry_async(messages, kwargs)
        return self._extract_text(response)

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Get the system prompt for this agent.

        Returns:
            The system prompt string.
        """
        pass
