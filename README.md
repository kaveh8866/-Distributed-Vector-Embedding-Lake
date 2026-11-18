# Distributed Vector & Embedding Lake

![Qdrant](https://img.shields.io/badge/vector%20engine-Qdrant-DC244C?logo=qdrant&logoColor=white)
![Python](https://img.shields.io/badge/service-Python%203.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/api-FastAPI-009688?logo=fastapi&logoColor=white)
![OpenTofu](https://img.shields.io/badge/IaC-OpenTofu-844FBA?logo=opentofu&logoColor=white)
![Stage](https://img.shields.io/badge/stage-1%20%C2%B7%20scaffold-blue)
![License](https://img.shields.io/badge/license-MIT-green)

A vector database and embedding pipeline for large-scale retrieval-augmented
generation. Documents are chunked, embedded and upserted into Qdrant; a
retrieval API serves nearest-neighbour search to RAG clients; an evaluation
loop reports recall@k and MRR so quality changes are measured rather than
assumed.

The stack is one Qdrant node plus a Python worker/API image, sized for a
high-memory cloud instance and a persistent block volume. Everything needed to
run it locally ships in this repository.

**Stage 1 status — scaffold complete:** the container topology, configuration
schema, ingestion worker, retrieval API and evaluation loop exist and validate.
The local stack was built and exercised end to end with the synthetic corpus
(see `docs/stage-1-verification.txt`). No OCI infrastructure has been created
yet; `terraform/` records the Stage 2 contract.

CloudGuard portability audit: **`PORTABLE_CERTIFIED` — 100/100 (5/5 checks)**,
audited 2026-11-18. The audit is structural (zero-leak, environment scaffold,
documentation, Git isolation, containerization); it does not certify a cloud
deployment.

---

## Architecture

```
 JSONL corpus
      |
      v
 ingest-worker ── chunk ── embed ── upsert ──────┐
 (one-shot batch)                                v
                                          +--------------+
                                          |    Qdrant    |
                                          |  segments    |
                                          |  WAL         |
                                          |  snapshots   |
                                          +--------------+
                                                 ^
 RAG client ── POST /search ── search-api ── embed ┘
                              (FastAPI)
```

| Component | Image | Responsibility | Default bind |
| --- | --- | --- | --- |
| `qdrant` | `qdrant/qdrant:v1.12.4` | Vector storage, HNSW index, snapshots | `127.0.0.1:6333` |
| `ingest-worker` | `cloudguard/vector-lake:local` | Chunk, embed, batched upsert | none (batch job) |
| `search-api` | `cloudguard/vector-lake:local` | Embed query, filtered search, JSON response | `127.0.0.1:8085` |

The worker and API share one image. Both run as uid/gid `10001` with a
read-only root filesystem, no added capabilities and `no-new-privileges`.

---

## Layout

```
docker/
  docker-compose.yml        canonical 3-service topology, limits and hardening
  qdrant/config.yaml        storage paths, listeners, payload policy
  worker/Dockerfile         shared worker/API image
src/vlake/
  config.py                 environment + YAML settings with validation
  embeddings.py             hashing and sentence-transformers providers
  ingestion.py              JSONL -> chunks -> vectors -> Qdrant
  search.py                 FastAPI retrieval service
  evaluation.py             recall@k / MRR evaluation loop
configs/
  vector_db.yaml            collection shape, HNSW, quantization
  embedding_models.yaml     supported models, dimensions, chunking rationale
  datasets/                 synthetic corpus and labelled evaluation queries
terraform/                  reserved OCI infrastructure contract (Stage 2)
tests/                      dependency-free unit tests
```

---

## Quickstart

Requirements: Docker Engine with Compose v2, 8 GB free RAM for the default
limits (lower `QDRANT_MEM_LIMIT` and `WORKER_MEM_LIMIT` in `.env` on a laptop).

```bash
cp .env.example .env
mkdir -p storage/qdrant storage/snapshots

# 1. start the database
docker compose --env-file .env -f docker/docker-compose.yml up -d qdrant

# 2. ingest the synthetic corpus (creates the collection on first run)
docker compose --env-file .env -f docker/docker-compose.yml run --rm ingest-worker

# 3. serve retrieval
docker compose --env-file .env -f docker/docker-compose.yml up -d search-api
```

The root `docker-compose.yml` extends the same services, so this is equivalent:

```bash
docker compose --env-file .env up -d --build
```

### Query it

```bash
curl -s http://127.0.0.1:8085/search \
  -H 'content-type: application/json' \
  -d '{"query": "how does quantization reduce memory", "limit": 3}'
```

```json
{
  "query": "how does quantization reduce memory",
  "collection": "rag_documents",
  "hits": [
    {
      "chunk_id": "doc-vector-quantization::0000",
      "doc_id": "doc-vector-quantization",
      "score": 0.71,
      "text": "Scalar quantization maps float32 vector components to int8 …"
    }
  ],
  "latency_ms": 4.2
}
```

### Evaluate retrieval

```bash
# Inside the image the working directory is /app, so use a relative path.
docker compose --env-file .env -f docker/docker-compose.yml run --rm \
  --entrypoint python ingest-worker -m vlake.evaluation \
  --dataset configs/datasets/eval_queries.jsonl --k 5
```

---

## How ingestion works

1. **Load** — `configs/datasets/*.jsonl`, one `{"id", "text", "metadata"}`
   object per line. Duplicate IDs and missing fields fail the run before any
   write.
2. **Chunk** — character windows (default 800, overlap 120) trimmed to
   whitespace boundaries, so a chunk never starts or ends mid-token.
3. **Embed** — the configured provider produces L2-normalised vectors.
4. **Upsert** — points are batched (default 64) and written with
   `wait=true`. Point IDs are UUIDv5 of `document-id::chunk-index`, which makes
   re-ingestion idempotent: the same chunk overwrites itself instead of
   duplicating.

Each point payload carries `doc_id`, `chunk_id`, `text`, `chunk_index`,
`chunk_count` and the document's own metadata, so retrieval can filter by
document and cite the source.

## Configuration

| File | Scope |
| --- | --- |
| `.env` (from `.env.example`) | endpoints, ports, resource limits, provider, chunking, log level |
| `configs/vector_db.yaml` | collection name, dimension, distance, HNSW, quantization, shards |
| `configs/embedding_models.yaml` | supported providers/models, dimensions, chunking rationale, eval settings |
| `docker/qdrant/config.yaml` | storage paths, listeners, payload on disk, telemetry off |

`VECTOR_SIZE` (environment) and `vector_size` (collection config) must match;
the worker refuses to start otherwise.

## Embedding providers

| Provider | Download | Quality | Use |
| --- | --- | --- | --- |
| `hashing` | none | lexical only | CI, container smoke tests, pipeline wiring |
| `sentence-transformers` | model weights | semantic | real retrieval; install the `models` extra |

```bash
pip install '.[models]'
EMBEDDING_PROVIDER=sentence-transformers \
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2 \
EMBEDDING_DIMENSION=384 VECTOR_SIZE=384 \
python -m vlake.ingestion --dataset configs/datasets/sample_documents.jsonl --recreate
```

The hashing provider exists so the pipeline can be built and tested offline. It
does **not** provide semantic retrieval; do not treat its scores as RAG quality.

## Security posture

- Only the retrieval API and the Qdrant HTTP port are published, both on
  `127.0.0.1`. Nothing is exposed on a public interface by default.
- Qdrant requires an API key; the value comes from the gitignored `.env`.
- Worker and API: read-only rootfs, `cap_drop: ALL`, `no-new-privileges`,
  `pids_limit`, non-root user, tmpfs for `/tmp`.
- Qdrant: `cap_drop: ALL`, `no-new-privileges`, no-new-privileges on every
  container, `pids_limit`, bounded log rotation (20 MiB x 3).
- Vector data, snapshots, `.env`, keys and Terraform state are gitignored. The
  tracked datasets are synthetic.

## Local checks (no container required)

```bash
python -m unittest discover -s tests -v                       # unit tests
PYTHONPATH=src python -m vlake.ingestion --dry-run \
  --dataset configs/datasets/sample_documents.jsonl           # chunking only
docker compose --env-file .env -f docker/docker-compose.yml config -q
```

## Roadmap

| Stage | Scope |
| --- | --- |
| 1 | Scaffold, container topology, ingestion/search/eval code, configs, metadata |
| 2 | OCI OpenTofu: VCN, NSG, A1.Flex / Standard3.Flex, persistent block volume, cloud-init |
| 3 | Live deployment and end-to-end verification on OCI with the real corpus |
| 4 | Scale-out: sharding, snapshot/restore drill, monitoring, capacity test |
| 5 | Retrieval quality and cost report (recall@k, MRR, latency, memory) |

## Honest limits

- Stage 1 has run locally only. There is no cloud deployment, no benchmark and
  no capacity claim yet.
- Single-node Qdrant with replication factor 1: no high availability.
- The synthetic corpus is ten short documents — enough to prove the pipeline,
  not to make quality claims.
- Quantization recall loss, HNSW parameter tuning and filtered-search latency
  are untested at scale.
- Snapshot/restore is documented and mounted but has not been drilled.

## License

MIT — Copyright (c) 2026 Mostafa Kaveh. See `LICENSE`.
