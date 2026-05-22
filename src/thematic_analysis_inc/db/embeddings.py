"""Embedding service and helpers for `Code.embedding` blobs.

`EmbeddingService` is the same class that used to live in
`thematic_analysis.codebook.embeddings`, moved here because embeddings now
live on DB rows (`Code.embedding`) rather than on a parallel in-memory
codebook. The class API is unchanged for the reviewer's use cases (embed,
embed_single).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from thematic_analysis_inc.db.models import Code

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class MockEmbeddingService:
    """Deterministic hash-based embeddings for tests (no model download)."""

    def __init__(self, embedding_dim: int = 384):
        self.embedding_dim = embedding_dim

    def _hash_to_embedding(self, text: str) -> NDArray[np.float32]:
        hash_bytes = hashlib.sha256(text.encode()).digest()
        np.random.seed(int.from_bytes(hash_bytes[:4], "big"))
        embedding = np.random.randn(self.embedding_dim).astype(np.float32)
        norm = float(np.linalg.norm(embedding)) + 1e-8
        return (embedding / norm).astype(np.float32)

    def embed(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.array([], dtype=np.float32).reshape(0, self.embedding_dim)
        return np.array(
            [self._hash_to_embedding(t) for t in texts], dtype=np.float32
        )

    def embed_single(self, text: str) -> NDArray[np.float32]:
        return self._hash_to_embedding(text)


class EmbeddingService:
    """Generates embeddings via Sentence Transformers (or a mock for tests)."""

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        use_mock: bool = False,
    ):
        self.model_name = model_name
        self.use_mock = use_mock
        self._model: SentenceTransformer | None = None
        self._mock: MockEmbeddingService | None = None

    @property
    def model(self) -> SentenceTransformer:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    @property
    def mock(self) -> MockEmbeddingService:
        if self._mock is None:
            self._mock = MockEmbeddingService()
        return self._mock

    def embed(self, texts: list[str]) -> NDArray[np.float32]:
        if self.use_mock:
            return self.mock.embed(texts)
        if not texts:
            return np.array([], dtype=np.float32)
        out: NDArray[np.float32] = self.model.encode(
            texts, convert_to_numpy=True
        )
        return out.astype(np.float32)

    def embed_single(self, text: str) -> NDArray[np.float32]:
        if self.use_mock:
            return self.mock.embed_single(text)
        out: NDArray[np.float32] = self.model.encode(
            [text], convert_to_numpy=True
        )
        return out[0].astype(np.float32)


def encode(arr: NDArray[np.float32]) -> bytes:
    """Pack a 1-D float32 array into the raw bytes we store in `Code.embedding`."""
    return arr.astype(np.float32, copy=False).tobytes()


def decode(buf: bytes) -> NDArray[np.float32]:
    """Reverse of :func:`encode`."""
    return np.frombuffer(buf, dtype=np.float32)


def _cosine_matrix(
    query: NDArray[np.float32], candidates: NDArray[np.float32]
) -> NDArray[np.float32]:
    if len(candidates) == 0:
        return np.array([], dtype=np.float32)
    q = query / (np.linalg.norm(query) + 1e-8)
    c = candidates / (np.linalg.norm(candidates, axis=1, keepdims=True) + 1e-8)
    return (c @ q).astype(np.float32)


def find_similar(
    query_emb: NDArray[np.float32],
    codes: list[Code],
    top_k: int,
) -> list[tuple[Code, float]]:
    """Top-k codes by cosine similarity over `Code.embedding`.

    Codes whose embedding is None are skipped (they can't be compared and
    should never appear in the reviewer's pool anyway — every reviewer Code
    is written with an embedding).
    """
    with_emb = [c for c in codes if c.embedding is not None]
    if not with_emb:
        return []
    mat = np.stack([decode(c.embedding) for c in with_emb])
    sims = _cosine_matrix(query_emb, mat)
    k = min(top_k, len(sims))
    top_idx = np.argsort(sims)[-k:][::-1]
    return [(with_emb[int(i)], float(sims[int(i)])) for i in top_idx]
