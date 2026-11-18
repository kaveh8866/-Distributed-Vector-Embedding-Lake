"""Retrieval API for the vector lake.

Endpoints:

``GET /health``   liveness only; never touches Qdrant
``GET /ready``    readiness: verifies the collection is reachable
``POST /search``  embed the query, run a filtered vector search, return payloads

The API holds one Qdrant client and one embedder per process. Both are created
lazily so a container can start (and fail its readiness probe) without
downloading a model.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import ConfigError, Settings
from .embeddings import Embedder, build_embedder

LOGGER = logging.getLogger("vlake.search")

# The container starts uvicorn directly, so no CLI code configures logging for
# this module. Without a root handler the warm-up and query INFO records are
# dropped and the container looks silent.
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Warm the embedding model so the first query is not a cold start.

    Loading an ONNX model on the first request adds hundreds of milliseconds to
    that request. Failure is logged, not fatal: readiness reports the real
    state and the API stays up for operators to inspect.
    """
    try:
        embedder().embed_query("warm-up")
        LOGGER.info("embedding model warmed up")
    except Exception as error:  # noqa: BLE001 - warm-up must not kill the service
        LOGGER.warning("embedding warm-up failed: %s", error)
    yield


app = FastAPI(
    title="Distributed Vector & Embedding Lake",
    version=__version__,
    description="Qdrant-backed retrieval API for RAG pipelines.",
    lifespan=lifespan,
)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    limit: int = Field(default=5, ge=1, le=50)
    doc_id: str | None = Field(default=None, description="Restrict results to one document")


class SearchHit(BaseModel):
    chunk_id: str
    doc_id: str
    score: float
    text: str


class SearchResponse(BaseModel):
    query: str
    collection: str
    hits: list[SearchHit]
    latency_ms: float


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def embedder() -> Embedder:
    current = settings()
    return build_embedder(current.embedding_provider, current.embedding_model, current.vector_size)


@lru_cache(maxsize=1)
def qdrant() -> Any:
    from qdrant_client import QdrantClient

    current = settings()
    kwargs: dict[str, Any] = {
        "url": current.qdrant_url,
        "prefer_grpc": False,
        "timeout": 30,
    }
    if current.qdrant_api_key:
        kwargs["api_key"] = current.qdrant_api_key
    return QdrantClient(**kwargs)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up and the package imported."""
    return {"status": "ok", "version": __version__}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness: Qdrant answers and the configured collection exists."""
    try:
        client = qdrant()
        collection = settings().collection
        if not client.collection_exists(collection):
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": f"collection {collection!r} is missing"},
            )
        return JSONResponse(status_code=200, content={"status": "ready", "collection": collection})
    except Exception as error:  # noqa: BLE001 - readiness must report, not raise
        LOGGER.warning("readiness check failed: %s", error)
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(error)})


@app.post("/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    """Return the nearest chunks for a query, optionally scoped to one document."""
    from qdrant_client import models

    current = settings()
    started = time.perf_counter()

    try:
        vector = embedder().embed_query(request.query)
    except ConfigError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    query_filter = None
    if request.doc_id:
        query_filter = models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=request.doc_id))]
        )

    try:
        result = qdrant().query_points(
            collection_name=current.collection,
            query=vector,
            limit=request.limit,
            with_payload=True,
            query_filter=query_filter,
        )
    except Exception as error:  # noqa: BLE001 - transport errors become 503
        LOGGER.error("vector search failed: %s", error)
        raise HTTPException(status_code=503, detail="vector database is unavailable") from error

    hits: list[SearchHit] = []
    for point in result.points:
        payload = point.payload or {}
        hits.append(
            SearchHit(
                chunk_id=str(payload.get("chunk_id", point.id)),
                doc_id=str(payload.get("doc_id", "unknown")),
                score=float(point.score),
                text=str(payload.get("text", "")),
            )
        )

    elapsed_ms = round((time.perf_counter() - started) * 1_000, 2)
    LOGGER.info("query served collection=%s hits=%d latency_ms=%.2f", current.collection, len(hits), elapsed_ms)
    return SearchResponse(query=request.query, collection=current.collection, hits=hits, latency_ms=elapsed_ms)


def main() -> None:  # pragma: no cover - entry point used by the container
    import uvicorn

    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8080"))
    uvicorn.run("vlake.search:app", host=host, port=port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
