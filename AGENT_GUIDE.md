# Agent Guide — Distributed Vector & Embedding Lake

This is the operating contract for an engineer or agent picking up the
repository. It states how to run the stack, where boundaries are, and what is
verified versus assumed.

## 1. What this project is

A Qdrant-backed vector and embedding lake for large-scale RAG:

- `ingest-worker` converts a JSONL corpus into chunks, embeddings and Qdrant
  points.
- `search-api` embeds a query and returns nearest chunks with payloads.
- `vlake.evaluation` measures recall@k and MRR against a labelled query set.

One image (`docker/worker/Dockerfile`) serves the worker and the API. Qdrant is
the only stateful service.

## 2. Repository boundaries

Never commit:

| Pattern | Reason |
| --- | --- |
| `.env`, `.env.*` except `.env.example` | endpoints, API keys |
| `storage/`, `snapshots/`, `*.snapshot`, `*.db`, `*.sqlite*` | vector data and payloads |
| `data/`, `datasets/raw/`, `datasets/private/` | non-synthetic corpora |
| `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*` | private keys |
| `*.tfstate*`, `*.tfvars`, `terraform.tfvars` | tenancy/compartment identifiers |
| `logs/`, `*.log` | may contain corpus excerpts and endpoints |

Rules:

- Verify with `git check-ignore -v <path>` and `git status --porcelain`, never
  by eye.
- `configs/datasets/*.jsonl` is synthetic and intentionally tracked. Private
  corpora go to `data/` and stay untracked.
- Local authority: `.env` and `terraform/terraform.tfvars` may be read to run
  the stack or `tofu apply`. Never print, copy or commit their values.

## 3. Local workflow

```bash
cp .env.example .env
mkdir -p storage/qdrant storage/snapshots

docker compose --env-file .env -f docker/docker-compose.yml config -q
docker compose --env-file .env -f docker/docker-compose.yml up -d qdrant
docker compose --env-file .env -f docker/docker-compose.yml run --rm ingest-worker
docker compose --env-file .env -f docker/docker-compose.yml up -d search-api

curl -s http://127.0.0.1:8085/health
curl -s http://127.0.0.1:8085/ready
curl -s http://127.0.0.1:8085/search \
  -H 'content-type: application/json' \
  -d '{"query": "chunk size and overlap", "limit": 3}'
```

Stop and remove everything local:

```bash
docker compose --env-file .env -f docker/docker-compose.yml down
```

That removes containers and the bridge network; `storage/` on the host keeps
the vectors. Delete `storage/` only when the corpus should be lost.

### Code checks without Docker

```bash
python -m unittest discover -s tests -v
PYTHONPATH=src python -m vlake.ingestion --dry-run \
  --dataset configs/datasets/sample_documents.jsonl
python -m compileall -q src tests
```

## 4. Configuration rules

- `VECTOR_SIZE` must equal the collection `vector_size` and the embedding model
  dimension. A mismatch is a startup error, not a warning.
- Changing `configs/vector_db.yaml` does not migrate an existing collection.
  Re-ingest with `--recreate` only when dropping the existing points is
  intended; state that intent before running it.
- `hashing` is the offline default. Switch to `sentence-transformers` (the
  `models` extra) for semantic retrieval.
- Resource limits live in `.env` and in `docker/docker-compose.yml`. The high-RAM
  profile assumes 8 GB for Qdrant and 4 GB for the worker; lower both on a
  laptop.

## 5. Evidence discipline

- Report the command and its output. `config -q` proves syntax, not retrieval
  quality.
- A local stack run proves the pipeline works on this machine; it says nothing
  about OCI capacity or performance.
- Record a stage's measured output under `docs/stage-N-verification.txt` before
  claiming the stage complete.
- Never present the hashing embedder's scores as semantic quality.

## 6. Git and release

- Isolated repository on `main`; remote is the project's GitHub repository.
- Commit messages describe why the change was made. No attribution trailers.
- Tag a release only when the stage's evidence is committed.

## 7. Stage roadmap

| Stage | Deliverable | Exit criteria |
| --- | --- | --- |
| 1 | Scaffold, compose topology, worker/API/eval code, configs | unit tests pass, compose validates, local pipeline runs |
| 2 | OCI OpenTofu: network, compute, block volume, cloud-init | `tofu validate`/`plan` clean, variables documented |
| 3 | Live OCI deployment | cloud-init done, healthy services, live ingest + search evidence |
| 4 | Scale and recovery | snapshot/restore drill, load test, monitoring |
| 5 | Quality and cost report | recall@k / MRR / latency / memory measured at target scale |

## 8. Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `/ready` returns 503 `collection ... is missing` | no ingestion has run | run `ingest-worker` |
| worker exits with a dimension error | `VECTOR_SIZE` ≠ model dimension | align `.env`, `configs/vector_db.yaml` and the model |
| Qdrant container restarts | storage path unwritable or bad config key | inspect `docker compose logs qdrant`, check `storage/` ownership |
| search returns nothing | empty collection or over-selective `doc_id` | check `client.count`, drop the filter |
| embedding model download fails | no egress or the `models` extra is missing | install the extra, verify egress |
| compose warns about `QDRANT_API_KEY` | `.env` missing | `cp .env.example .env` |
