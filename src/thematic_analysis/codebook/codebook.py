"""Adaptive codebook for storing and managing codes."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from thematic_analysis.codebook.embeddings import EmbeddingService

if TYPE_CHECKING:
    from thematic_analysis.research_context import ResearchContext


@dataclass
class Quote:
    """A quote from the data associated with a code."""

    quote_id: str
    text: str


@dataclass
class CodeEntry:
    """An entry in the codebook representing a code with its quotes."""

    code: str
    quotes: list[Quote] = field(default_factory=list)
    embedding: NDArray[np.float32] | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "code": self.code,
            "quotes": [{"quote_id": q.quote_id, "text": q.text} for q in self.quotes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CodeEntry":
        """Create CodeEntry from dictionary."""
        quotes = [Quote(quote_id=q["quote_id"], text=q["text"]) for q in data["quotes"]]
        return cls(code=data["code"], quotes=quotes)


class Codebook:
    """Adaptive codebook for storing codes and their associated quotes.

    The codebook maintains codes, their quotes, and embeddings for
    similarity-based retrieval.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService | None = None,
        max_quotes_per_code: int = 20,
        use_mock_embeddings: bool = False,
        research_context: "ResearchContext | None" = None,
    ):
        """Initialize the codebook.

        Args:
            embedding_service: Service for generating embeddings.
            max_quotes_per_code: Maximum quotes to keep per code.
            use_mock_embeddings: If True, use mock embeddings (fast, for testing).
            research_context: Research context pinned to this codebook
                revision. Agents read it off the codebook rather than as a
                separate argument.
        """
        self.embedding_service = embedding_service or EmbeddingService(
            use_mock=use_mock_embeddings
        )
        self.max_quotes_per_code = max_quotes_per_code
        self._entries: list[CodeEntry] = []
        self.research_context = research_context

    @property
    def entries(self) -> list[CodeEntry]:
        """Get all code entries."""
        return self._entries

    @property
    def codes(self) -> list[str]:
        """Get all code texts."""
        return [entry.code for entry in self._entries]

    def __len__(self) -> int:
        """Return number of codes in the codebook."""
        return len(self._entries)

    def copy(self) -> "Codebook":
        """Create a deep copy of this codebook.

        Each coder should work with an independent copy to prevent shared state.
        Per the paper: 'Each coder often works independently to generate codes.'

        Returns:
            A new Codebook instance with copied entries.
        """
        import copy as copy_module

        new_codebook = Codebook(
            embedding_service=self.embedding_service,
            max_quotes_per_code=self.max_quotes_per_code,
        )
        # Deep copy entries to prevent shared state
        for entry in self._entries:
            new_entry = CodeEntry(
                code=entry.code,
                quotes=[Quote(q.quote_id, q.text) for q in entry.quotes],
                embedding=copy_module.deepcopy(entry.embedding),
            )
            new_codebook._entries.append(new_entry)
        return new_codebook

    def _get_embeddings_matrix(self) -> NDArray[np.float32] | None:
        """Get embeddings matrix from entries."""
        embeddings = [e.embedding for e in self._entries if e.embedding is not None]
        if not embeddings:
            return None
        return np.stack(embeddings)

    def add_code(self, code: str, quotes: list[Quote]) -> CodeEntry:
        """Add a new code to the codebook.

        Args:
            code: The code text/label.
            quotes: List of quotes associated with this code.

        Returns:
            The created CodeEntry.
        """
        if len(quotes) > self.max_quotes_per_code:
            quotes = quotes[: self.max_quotes_per_code]

        embedding = self.embedding_service.embed_single(code)
        entry = CodeEntry(code=code, quotes=quotes, embedding=embedding)
        self._entries.append(entry)
        return entry

    def add_quotes_to_code(self, code_index: int, quotes: list[Quote]) -> None:
        """Add quotes to an existing code.

        Args:
            code_index: Index of the code entry.
            quotes: Quotes to add.
        """
        if code_index < 0 or code_index >= len(self._entries):
            raise IndexError(f"Invalid code index: {code_index}")

        entry = self._entries[code_index]
        existing_ids = {q.quote_id for q in entry.quotes}

        for quote in quotes:
            if quote.quote_id not in existing_ids:
                entry.quotes.append(quote)
                existing_ids.add(quote.quote_id)

        if len(entry.quotes) > self.max_quotes_per_code:
            entry.quotes = entry.quotes[: self.max_quotes_per_code]

    def find_similar_codes(
        self, code: str, top_k: int = 10
    ) -> list[tuple[CodeEntry, float]]:
        """Find the most similar codes to a given code.

        Args:
            code: The code text to find similar codes for.
            top_k: Number of top similar codes to return.

        Returns:
            List of (CodeEntry, similarity_score) tuples sorted by similarity.
        """
        embeddings = self._get_embeddings_matrix()
        if embeddings is None:
            return []

        query_embedding = self.embedding_service.embed_single(code)
        similar_indices = self.embedding_service.find_similar(
            query_embedding, embeddings, top_k
        )

        return [(self._entries[idx], score) for idx, score in similar_indices]

    def merge_codes(
        self, source_index: int, target_index: int, new_code: str | None = None
    ) -> None:
        """Merge source code into target code.

        Args:
            source_index: Index of code to merge from.
            target_index: Index of code to merge into.
            new_code: Optional new code text for merged code.
        """
        if source_index == target_index:
            return

        if source_index < 0 or source_index >= len(self._entries):
            raise IndexError(f"Invalid source index: {source_index}")
        if target_index < 0 or target_index >= len(self._entries):
            raise IndexError(f"Invalid target index: {target_index}")

        source = self._entries[source_index]
        target = self._entries[target_index]

        if new_code:
            target.code = new_code
            target.embedding = self.embedding_service.embed_single(new_code)

        # We add quotes BEFORE popping, so target_index is still valid
        self.add_quotes_to_code(target_index, source.quotes)
        self._entries.pop(source_index)

    def update_code(self, index: int, new_code: str) -> None:
        """Update a code's text.

        Args:
            index: Index of the code entry.
            new_code: New code text.
        """
        if index < 0 or index >= len(self._entries):
            raise IndexError(f"Invalid code index: {index}")

        self._entries[index].code = new_code
        self._entries[index].embedding = self.embedding_service.embed_single(new_code)

    def to_json(self) -> str:
        """Serialize codebook to JSON string."""
        data = {"codes": [entry.to_dict() for entry in self._entries]}
        return json.dumps(data, indent=2)

    def to_dict(self) -> dict:
        """Convert codebook to dictionary."""
        return {"codes": [entry.to_dict() for entry in self._entries]}

    @classmethod
    def from_json(
        cls,
        json_str: str,
        embedding_service: EmbeddingService | None = None,
        max_quotes_per_code: int = 20,
        use_mock_embeddings: bool = False,
    ) -> "Codebook":
        """Deserialize codebook from JSON string."""
        data = json.loads(json_str)
        codebook = cls(
            embedding_service=embedding_service,
            max_quotes_per_code=max_quotes_per_code,
            use_mock_embeddings=use_mock_embeddings,
        )

        for code_data in data.get("codes", []):
            entry = CodeEntry.from_dict(code_data)
            entry.embedding = codebook.embedding_service.embed_single(entry.code)
            codebook._entries.append(entry)

        return codebook

    def save(self, path: str | Path) -> None:
        """Save codebook to a JSON file."""
        Path(path).write_text(self.to_json())

    @classmethod
    def load(
        cls,
        path: str | Path,
        embedding_service: EmbeddingService | None = None,
        max_quotes_per_code: int = 20,
        use_mock_embeddings: bool = False,
    ) -> "Codebook":
        """Load codebook from a JSON file."""
        return cls.from_json(
            Path(path).read_text(),
            embedding_service=embedding_service,
            max_quotes_per_code=max_quotes_per_code,
            use_mock_embeddings=use_mock_embeddings,
        )
