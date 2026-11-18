# syntax=docker/dockerfile:1.7
# =============================================================================
# Distributed Vector & Embedding Lake — worker / API image
# =============================================================================
# One image serves both roles so the embedding stack is built once:
#   ingest worker  python -m vlake.ingestion
#   retrieval API  python -m uvicorn vlake.search:app
#
# Build context is the repository root:
#   docker build -f docker/worker/Dockerfile -t cloudguard/vector-lake:local .
#
# The runtime user is unprivileged (uid/gid 10001), the rootfs is read-only in
# Compose, and no model weights are baked in: the default embedder is the
# offline deterministic hashing provider. Semantic providers install through a
# build arg and cache their weights in the mounted /cache volume:
#
#   docker build -f docker/worker/Dockerfile \
#     --build-arg VLAKE_EXTRAS=[semantic] -t cloudguard/vector-lake:local .

FROM python:3.12-slim

LABEL org.opencontainers.image.title="CloudGuard Distributed Vector & Embedding Lake" \
      org.opencontainers.image.description="Embedding ingestion worker and RAG retrieval API for Qdrant" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/kaveh8866/-Distributed-Vector-Embedding-Lake"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VLAKE_CONFIG_DIR=/app/configs

# No compilers or build tooling are installed; every dependency ships a wheel.
RUN groupadd --gid 10001 vlake \
 && useradd --uid 10001 --gid vlake --no-create-home --shell /usr/sbin/nologin vlake

WORKDIR /app

# Dependency layer first so source edits do not invalidate installed packages.
COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs

# Optional dependency groups (for example [semantic] for ONNX embeddings).
# "base" means no extras: the image stays small and offline-friendly.
ARG VLAKE_EXTRAS="base"

ENV FASTEMBED_CACHE_PATH=/cache/fastembed \
    HF_HOME=/cache/huggingface

# /cache is a named volume in Compose; creating it here fixes its ownership so
# the unprivileged user can write model weights into it.
RUN mkdir -p /cache/fastembed /cache/huggingface \
 && chown -R 10001:10001 /cache \
 && if [ "$VLAKE_EXTRAS" = "base" ]; then spec="."; else spec=".${VLAKE_EXTRAS}"; fi \
 && pip install --no-cache-dir "$spec" \
 && rm -rf /root/.cache /tmp/*

USER 10001:10001

ENTRYPOINT ["python"]
CMD ["-m", "vlake.ingestion", "--help"]
