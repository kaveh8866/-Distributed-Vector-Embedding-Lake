"""Ingestion pipeline: documents -> chunks -> embeddings -> Qdrant.

The worker is deliberately streaming and idempotent. Chunk IDs are derived from
``document id + chunk index``, so re-running an ingestion over the same corpus
overwrites the same points instead of duplicating them.

Qdrant and the embedding model are imported lazily: ``--dry-run`` and the unit
tests work with the standard library only.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .config import CollectionConfig, ConfigError, Settings
from .embeddings import Embedder, build_embedder

LOGGER = logging.getLogger("vlake.ingestion")

DEFAULT_DATASET = "sample_documents.jsonl"


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 120) -> list[str]:
    """Split text into overlapping windows, preferring whitespace boundaries.

    ``chunk_size`` and ``overlap`` are measured in characters. A window that
    would end mid-word is trimmed back to the last space, so no chunk begins or
    ends inside a token. The final chunk is always included.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and smaller than chunk_size")

    normalized = " ".join(text.split())
    if not normalized:
        return []
    if len(normalized) <= chunk_size:
        return [normalized]

    chunks: list[str] = []
    step = chunk_size - overlap
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind(" ", start + step, end)
            if boundary > start:
                end = boundary
        candidate = normalized[start:end].strip()
        if candidate:
            chunks.append(candidate)
        if end >= len(normalized):
            break
        start = max(end - overlap, start + 1)

    return chunks


def chunk_document(document: Document, chunk_size: int = 800, overlap: int = 120) -> list[Chunk]:
    """Chunk one document, producing stable IDs and inherited metadata."""
    pieces = chunk_text(document.text, chunk_size=chunk_size, overlap=overlap)
    chunks: list[Chunk] = []
    for index, piece in enumerate(pieces):
        chunks.append(
            Chunk(
                chunk_id=f"{document.doc_id}::{index:04d}",
                doc_id=document.doc_id,
                text=piece,
                metadata={**document.metadata, "chunk_index": index, "chunk_count": len(pieces)},
            )
        )
    return chunks


def load_documents(path: str | Path) -> list[Document]:
    """Read a JSONL corpus: one object per line with ``id`` and ``text``."""
    target = Path(path)
    if not target.is_file():
        raise ConfigError(f"dataset not found: {target}")

    documents: list[Document] = []
    seen: set[str] = set()

    with target.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise ConfigError(f"{target}:{line_number} is not valid JSON: {error.msg}") from error

            if not isinstance(payload, dict):
                raise ConfigError(f"{target}:{line_number} must be a JSON object")

            doc_id = payload.get("id") or payload.get("doc_id")
            text = payload.get("text")
            if not isinstance(doc_id, str) or not doc_id.strip():
                raise ConfigError(f"{target}:{line_number} needs a non-empty 'id'")
            if not isinstance(text, str) or not text.strip():
                raise ConfigError(f"{target}:{line_number} needs non-empty 'text'")
            if doc_id in seen:
                raise ConfigError(f"{target}:{line_number} repeats id {doc_id!r}")
            seen.add(doc_id)

            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ConfigError(f"{target}:{line_number} 'metadata' must be an object")

            documents.append(Document(doc_id=doc_id, text=text, metadata=metadata))

    if not documents:
        raise ConfigError(f"{target} contains no documents")
    return documents


def point_id(chunk_id: str) -> str:
    """Map a readable chunk ID to the UUID Qdrant requires."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


def build_client(settings: Settings) -> Any:
    """Create a Qdrant client. Imported lazily so dry runs need no dependency."""
    from qdrant_client import QdrantClient

    kwargs: dict[str, Any] = {
        "url": settings.qdrant_url,
        "prefer_grpc": False,
        "timeout": 30,
    }
    if settings.qdrant_api_key:
        kwargs["api_key"] = settings.qdrant_api_key
    return QdrantClient(**kwargs)


def ensure_collection(
    client: Any,
    collection: CollectionConfig,
    *,
    recreate: bool = False,
) -> bool:
    """Create the collection from configs/vector_db.yaml when it is absent.

    Returns ``True`` when the collection was created. An existing collection is
    never modified implicitly: schema changes are explicit operator actions.
    """
    from qdrant_client import models

    exists = client.collection_exists(collection.name)
    if exists and not recreate:
        LOGGER.info("collection %s already exists", collection.name)
        return False
    if exists and recreate:
        LOGGER.warning("recreating collection %s; existing points are dropped", collection.name)
        client.delete_collection(collection.name)

    try:
        distance = getattr(models.Distance, collection.distance.upper())
    except AttributeError as error:
        raise ConfigError(f"unsupported DISTANCE={collection.distance!r}") from error

    quantization = None
    if collection.quantization == "scalar_int8":
        quantization = models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8,
                quantile=0.99,
                always_ram=True,
            )
        )

    client.create_collection(
        collection_name=collection.name,
        vectors_config=models.VectorParams(
            size=collection.vector_size,
            distance=distance,
            on_disk=True,
        ),
        hnsw_config=models.HnswConfigDiff(
            m=collection.hnsw_m,
            ef_construct=collection.hnsw_ef_construct,
            full_scan_threshold=collection.full_scan_threshold_kb,
        ),
        quantization_config=quantization,
        replication_factor=collection.replication_factor,
        shard_number=collection.shard_number,
    )
    LOGGER.info(
        "created collection %s (%d dims, %s, %s quantization)",
        collection.name,
        collection.vector_size,
        collection.distance,
        collection.quantization,
    )
    return True


def _payload(chunk: Chunk) -> dict[str, Any]:
    return {
        **chunk.metadata,
        "doc_id": chunk.doc_id,
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
    }


def upsert_chunks(
    client: Any,
    embedder: Embedder,
    chunks: Sequence[Chunk],
    *,
    collection: str,
    batch_size: int = 64,
) -> int:
    """Embed and upsert chunks in batches; returns the number of points written."""
    from qdrant_client import models

    written = 0
    for offset in range(0, len(chunks), batch_size):
        batch = chunks[offset : offset + batch_size]
        vectors = embedder.embed_documents([chunk.text for chunk in batch])
        if len(vectors) != len(batch):
            raise RuntimeError("embedding provider returned a mismatched batch")
        points = [
            models.PointStruct(id=point_id(chunk.chunk_id), vector=vector, payload=_payload(chunk))
            for chunk, vector in zip(batch, vectors)
        ]
        client.upsert(collection_name=collection, points=points, wait=True)
        written += len(points)
        LOGGER.info("upserted %d/%d chunks", written, len(chunks))
    return written


def run_ingestion(
    settings: Settings,
    dataset_path: str | Path,
    *,
    collection_config: CollectionConfig | None = None,
    recreate: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute (or plan) one ingestion pass and return a JSON-safe summary."""
    documents = load_documents(dataset_path)
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(
            chunk_document(document, chunk_size=settings.chunk_size, overlap=settings.chunk_overlap)
        )

    summary: dict[str, Any] = {
        "dataset": str(dataset_path),
        "documents": len(documents),
        "chunks": len(chunks),
        "average_chunk_chars": round(sum(len(chunk.text) for chunk in chunks) / len(chunks), 1),
        "embedding_provider": settings.embedding_provider,
        "dry_run": dry_run,
    }

    if dry_run:
        LOGGER.info("dry run: %d documents -> %d chunks", len(documents), len(chunks))
        return summary

    config = collection_config or CollectionConfig.from_file()
    if config.vector_size != settings.vector_size:
        raise ConfigError(
            f"VECTOR_SIZE={settings.vector_size} does not match collection vector_size={config.vector_size}"
        )

    embedder = build_embedder(settings.embedding_provider, settings.embedding_model, settings.vector_size)
    summary["embedder"] = embedder.name
    summary["vector_dimension"] = embedder.dimension

    client = build_client(settings)
    created = ensure_collection(client, config, recreate=recreate)
    summary["collection"] = config.name
    summary["collection_created"] = created

    written = upsert_chunks(
        client,
        embedder,
        chunks,
        collection=config.name,
        batch_size=settings.batch_size,
    )
    summary["points_written"] = written
    summary["collection_points"] = int(client.count(collection_name=config.name, exact=True).count)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a JSONL corpus into the vector lake.")
    parser.add_argument(
        "--dataset",
        default=str(Path("configs") / "datasets" / DEFAULT_DATASET),
        help="JSONL corpus (default: configs/datasets/sample_documents.jsonl)",
    )
    parser.add_argument("--dry-run", action="store_true", help="chunk and count without writing")
    parser.add_argument("--recreate", action="store_true", help="drop and rebuild the collection")
    parser.add_argument("--log-level", default=None, help="override LOG_LEVEL")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, (args.log_level or settings.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        summary = run_ingestion(
            settings,
            args.dataset,
            recreate=args.recreate,
            dry_run=args.dry_run,
        )
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - CLI boundary reports and exits non-zero
        LOGGER.error("ingestion failed: %s", error)
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
