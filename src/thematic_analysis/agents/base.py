"""Base agent class for all thematic analysis agents."""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass

from openhands.sdk import LLM, Message, TextContent


@dataclass
class AgentConfig:
    """Configuration for an agent."""

    model: str = "anthropic/claude-sonnet-4-20250514"
    temperature: float = 0.7
    max_tokens: int = 4096
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
        """Lazy load the LLM instance."""
        if self._llm is None:
            # Use SDK's load_from_env which handles OpenHands proxy correctly
            self._llm = LLM.load_from_env()
            # Override with config settings if specified
            if self.config.temperature is not None:
                self._llm.temperature = self.config.temperature
            if self.config.max_tokens is not None:
                self._llm.max_output_tokens = self.config.max_tokens
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
        response = self.llm.completion(messages=messages, **kwargs)
        return self._extract_text(response)

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
        # Run sync completion in thread pool for async compatibility
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: self.llm.completion(messages=messages, **kwargs)
        )
        return self._extract_text(response)

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Get the system prompt for this agent.

        Returns:
            The system prompt string.
        """
        pass
