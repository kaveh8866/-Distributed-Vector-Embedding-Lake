"""Configuration for the vector and embedding lake.

Runtime settings come from environment variables so the same image runs as an
ingestion worker, a retrieval API or a one-off evaluation job. Declarative
parameters (collection shape, HNSW, quantization, embedding models, sample
data) live in the tracked YAML files under ``configs/``.

No secret is read from a file: the Qdrant API key arrives through the
environment, which Compose injects from the gitignored ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pyyaml is a runtime dependency; a few unit tests import this module.
    import yaml
except ModuleNotFoundError:  # pragma: no cover - only without dependencies
    yaml = None  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Raised when configuration is missing or internally inconsistent."""


def config_dir() -> Path:
    """Directory holding the tracked YAML configuration."""
    override = os.environ.get("VLAKE_CONFIG_DIR")
    return Path(override).expanduser().resolve() if override else _REPO_ROOT / "configs"


def _text(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _integer(name: str, default: int) -> int:
    raw = _text(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from error


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load one YAML document and require a mapping at the top level."""
    if yaml is None:  # pragma: no cover - dependency is declared
        raise ConfigError("pyyaml is required to read configuration files")

    target = Path(path)
    if not target.is_file():
        raise ConfigError(f"configuration file not found: {target}")
    try:
        parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML in {target}: {error}") from error
    if not isinstance(parsed, dict):
        raise ConfigError(f"{target} must contain a mapping at the top level")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment-driven runtime settings."""

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str | None = None
    collection: str = "rag_documents"
    vector_size: int = 384
    distance: str = "Cosine"
    embedding_provider: str = "hashing"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    batch_size: int = 64
    chunk_size: int = 800
    chunk_overlap: int = 120
    top_k: int = 5
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        # With slots=True, defaults must be read from an instance; class
        # attributes are member descriptors, not values.
        defaults = cls()
        settings = cls(
            qdrant_url=_text("QDRANT_URL", defaults.qdrant_url) or defaults.qdrant_url,
            qdrant_api_key=_text("QDRANT_API_KEY"),
            collection=_text("COLLECTION_NAME", defaults.collection) or defaults.collection,
            vector_size=_integer("VECTOR_SIZE", defaults.vector_size),
            distance=_text("DISTANCE", defaults.distance) or defaults.distance,
            embedding_provider=(_text("EMBEDDING_PROVIDER", defaults.embedding_provider) or "").lower(),
            embedding_model=_text("EMBEDDING_MODEL", defaults.embedding_model) or defaults.embedding_model,
            batch_size=_integer("INGEST_BATCH_SIZE", defaults.batch_size),
            chunk_size=_integer("CHUNK_SIZE", defaults.chunk_size),
            chunk_overlap=_integer("CHUNK_OVERLAP", defaults.chunk_overlap),
            top_k=_integer("TOP_K", defaults.top_k),
            log_level=(_text("LOG_LEVEL", defaults.log_level) or defaults.log_level).upper(),
        )
        return settings.validate()

    def validate(self) -> "Settings":
        if not self.qdrant_url.startswith(("http://", "https://")):
            raise ConfigError("QDRANT_URL must start with http:// or https://")
        if self.vector_size <= 0:
            raise ConfigError("VECTOR_SIZE must be positive")
        if self.chunk_size <= 0:
            raise ConfigError("CHUNK_SIZE must be positive")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ConfigError("CHUNK_OVERLAP must be >= 0 and smaller than CHUNK_SIZE")
        if self.batch_size <= 0:
            raise ConfigError("INGEST_BATCH_SIZE must be positive")
        if not 1 <= self.top_k <= 100:
            raise ConfigError("TOP_K must be between 1 and 100")
        return self


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    """Per-collection vector and index parameters from configs/vector_db.yaml."""

    name: str = "rag_documents"
    vector_size: int = 384
    distance: str = "Cosine"
    hnsw_m: int = 16
    hnsw_ef_construct: int = 128
    full_scan_threshold_kb: int = 10_000
    on_disk_payload: bool = True
    quantization: str = "scalar_int8"
    replication_factor: int = 1
    shard_number: int = 1

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "CollectionConfig":
        target = Path(path) if path else config_dir() / "vector_db.yaml"
        document = load_yaml(target)
        collection = document.get("collection")
        if not isinstance(collection, dict):
            raise ConfigError(f"{target} has no 'collection' mapping")

        hnsw = collection.get("hnsw") if isinstance(collection.get("hnsw"), dict) else {}
        payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
        quantization = (
            collection.get("quantization") if isinstance(collection.get("quantization"), dict) else {}
        )

        # slots=True: read defaults from an instance, not from class descriptors.
        defaults = cls()
        return cls(
            name=str(collection.get("name", defaults.name)),
            vector_size=int(collection.get("vector_size", defaults.vector_size)),
            distance=str(collection.get("distance", defaults.distance)),
            hnsw_m=int(hnsw.get("m", defaults.hnsw_m)),
            hnsw_ef_construct=int(hnsw.get("ef_construct", defaults.hnsw_ef_construct)),
            full_scan_threshold_kb=int(
                hnsw.get("full_scan_threshold_kb", defaults.full_scan_threshold_kb)
            ),
            on_disk_payload=bool(payload.get("on_disk", defaults.on_disk_payload)),
            quantization=str(quantization.get("type", defaults.quantization)),
            replication_factor=int(collection.get("replication_factor", defaults.replication_factor)),
            shard_number=int(collection.get("shard_number", defaults.shard_number)),
        )
