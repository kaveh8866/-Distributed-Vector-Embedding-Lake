"""Embedding providers for the vector lake.

Three providers are supported:

``hashing``
    A deterministic, dependency-free token-hashing embedder. It needs no model
    download, produces identical vectors on every machine and is what the unit
    tests use. It captures lexical similarity only, so it is *not* a semantic
    RAG embedding.

``fastembed``
    ONNX sentence embeddings (BAAI/bge-*, all-MiniLM-*) through the optional
    ``semantic`` extra. Small download, CPU-only, no PyTorch. This is the
    provider used for semantic retrieval verification.

``sentence-transformers``
    A PyTorch transformer encoder from the optional ``models`` extra, for
    GPUs and larger models.

All providers return L2-normalised Python floats, which is what qdrant-client
serialises.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Sequence

from .config import ConfigError

_TOKEN_RE = re.compile(r"[a-z0-9]+")
SUPPORTED_PROVIDERS = ("hashing", "fastembed", "sentence-transformers")


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


def _normalise(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return values
    return [value / norm for value in values]


class FastEmbedEmbedder(Embedder):
    """ONNX sentence embeddings via the optional ``semantic`` extra.

    FastEmbed keeps the image small (no PyTorch) and runs on CPU, which is what
    the A1.Flex certification target offers. Weights download once into the
    cache directory (`FASTEMBED_CACHE_PATH` in the container) and are reused.
    """

    def __init__(self, model_name: str, dimension: int | None = None, cache_dir: str | None = None) -> None:
        try:
            from fastembed import TextEmbedding
        except ModuleNotFoundError as error:  # pragma: no cover - optional extra
            raise ConfigError(
                "EMBEDDING_PROVIDER=fastembed requires the 'semantic' extra: "
                "install '.[semantic]' or set VLAKE_EXTRAS=[semantic] at image build time"
            ) from error

        # ONNX Runtime defaults to one worker thread per host core and spins
        # between tasks. Inside a container with a CPU quota that oversubscribes
        # the quota and the kernel throttles each request to the cgroup period
        # (measured: 100/200/300 ms steps). One thread per allocated CPU keeps
        # latency predictable.
        try:
            threads = max(1, int(os.environ.get("FASTEMBED_THREADS", "1")))
        except ValueError:
            threads = 1

        kwargs: dict[str, object] = {"threads": threads}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        self._model = TextEmbedding(model_name=model_name, **kwargs)
        self.name = f"fastembed:{model_name}"

        probe = next(iter(self._model.embed(["dimension probe"])))
        self.dimension = int(len(probe))
        if dimension and dimension != self.dimension:
            raise ConfigError(
                f"EMBEDDING_DIMENSION/VECTOR_SIZE={dimension} does not match {model_name} ({self.dimension})"
            )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [_normalise(vector) for vector in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        # Retrieval models such as bge apply a query instruction prefix; use the
        # model's own query path when it provides one.
        query_embed = getattr(self._model, "query_embed", None)
        vectors = query_embed(text) if callable(query_embed) else self._model.embed([text])
        return _normalise(next(iter(vectors)))


def build_embedder(provider: str, model: str, dimension: int | None = None) -> Embedder:
    """Construct the configured provider, failing loudly on an unknown name."""
    normalized = (provider or "").strip().lower()
    if normalized == "hashing":
        return HashingEmbedder(dimension or 384)
    if normalized in {"fastembed", "onnx", "onnx-fastembed"}:
        cache_dir = os.environ.get("FASTEMBED_CACHE_PATH") or None
        return FastEmbedEmbedder(model, dimension, cache_dir=cache_dir)
    if normalized in {"sentence-transformers", "sentence_transformers", "st"}:
        return SentenceTransformerEmbedder(model, dimension)
    raise ConfigError(
        f"unknown EMBEDDING_PROVIDER={provider!r}; expected one of {', '.join(SUPPORTED_PROVIDERS)}"
    )
