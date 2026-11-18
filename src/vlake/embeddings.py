"""Embedding providers for the vector lake.

Two providers are supported:

``hashing``
    A deterministic, dependency-free token-hashing embedder. It needs no model
    download, produces identical vectors on every machine and is what the
    container smoke test and unit tests use. It captures lexical similarity
    only, so it is *not* a production RAG embedding.

``sentence-transformers``
    A real transformer encoder loaded lazily from the optional ``models``
    extra. Vectors are L2-normalised so cosine and dot product agree.

Both return Python floats, which is what qdrant-client serialises.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Sequence

from .config import ConfigError

_TOKEN_RE = re.compile(r"[a-z0-9]+")
SUPPORTED_PROVIDERS = ("hashing", "sentence-transformers")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens; stable across Python versions and machines."""
    return _TOKEN_RE.findall(text.lower())


class Embedder(ABC):
    """Minimal embedding interface shared by the worker and the API."""

    name: str
    dimension: int

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of documents."""

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class HashingEmbedder(Embedder):
    """Signed feature hashing over unigrams and bigrams.

    The vector is deterministic (blake2b, not ``hash()``) and L2-normalised.
    Collisions are inherent to hashing; a 384- or 768-dimensional space keeps
    them tolerable for smoke tests and evaluation harness wiring.
    """

    def __init__(self, dimension: int = 384) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = dimension
        self.name = f"hashing-{dimension}"

    def _vector(self, text: str) -> list[float]:
        tokens = tokenize(text)
        if not tokens:
            return [0.0] * self.dimension

        features: Counter[str] = Counter(tokens)
        features.update(f"{left}_{right}" for left, right in zip(tokens, tokens[1:]))

        vector = [0.0] * self.dimension
        for feature, count in features.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class SentenceTransformerEmbedder(Embedder):
    """Transformer encoder from the optional ``models`` dependency group."""

    def __init__(self, model_name: str, dimension: int | None = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ModuleNotFoundError as error:  # pragma: no cover - optional extra
            raise ConfigError(
                "EMBEDDING_PROVIDER=sentence-transformers requires the 'models' extra: "
                "pip install '.[models]'"
            ) from error

        self.name = model_name
        self._model = SentenceTransformer(model_name)
        detected = int(self._model.get_sentence_embedding_dimension())
        self.dimension = dimension or detected
        if self.dimension != detected:
            raise ConfigError(
                f"EMBEDDING_DIMENSION={self.dimension} does not match {model_name} ({detected})"
            )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(
            list(texts),
            batch_size=32,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in row] for row in vectors]


def build_embedder(provider: str, model: str, dimension: int | None = None) -> Embedder:
    """Construct the configured provider, failing loudly on an unknown name."""
    normalized = (provider or "").strip().lower()
    if normalized == "hashing":
        return HashingEmbedder(dimension or 384)
    if normalized in {"sentence-transformers", "sentence_transformers", "st"}:
        return SentenceTransformerEmbedder(model, dimension)
    raise ConfigError(
        f"unknown EMBEDDING_PROVIDER={provider!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}"
    )
