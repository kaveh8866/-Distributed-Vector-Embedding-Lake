# Agent Guide — Distributed Vector & Embedding Lake

Operating contract for an AI coding agent (Cursor, Claude Code, Windsurf, or a
terminal agent) picking up this repository. It states what to run, in what
order, what healthy looks like, and what is verified versus assumed.

> **Read section 13 before making any availability or certification claim.** The
> stack is verified locally (Stage 4) and deployed on OCI (Stage 5). The live
> claims in this repository are limited to the evidence in
> `docs/stage-5-verification.txt` — a single A2.Flex node with a 10-document
> corpus, not scale or availability certification.

## 1. Current state

| Layer | Status | Evidence |
| --- | --- | --- |
| Container topology, worker, API, evaluation | Verified locally | `docs/stage-1-verification.txt` (live Qdrant, recall@5 0.875 lexical) |
| Schema init, semantic ingestion, retrieval, latency | Verified locally (Stage 4) | `docs/stage-4-verification.txt`, `docs/stage-4-report.json` (21 unit tests, recall@5 1.0, MRR 1.0, p95 8.41 ms) |
| OCI infrastructure modules | **Applied** (8 resources, eu-frankfurt-1) | `docs/stage-5-verification.txt` (apply output, IDs, security list) |
| Live OCI deployment | Verified 2026-11-18 on `VM.Standard.A2.Flex` | `docs/stage-5-verification.txt`, `docs/stage-5-report.json` (healthy host, 10 points, recall@5 1.0, p95 11.35 ms) |

The local stack was stopped after verification. `storage/` (vector segments) is
retained on the workstation and gitignored. No OCI resource exists.

## 2. What this project is

A Qdrant-backed vector and embedding lake for large-scale RAG:

- `ingest-worker` converts a JSONL corpus into chunks, embeddings and points.
- `search-api` embeds a query and returns nearest chunks with payloads.
- `vlake.evaluation` reports recall@k and MRR against a labelled query set.

One image (`docker/worker/Dockerfile`) serves the worker and the API. Qdrant is
the only stateful service.

```
JSONL corpus → ingest-worker → chunk → embed → upsert → Qdrant (segments, WAL, snapshots)
RAG client  → POST /search  → search-api → embed → query_points → hits + payloads
```

## 3. Repository map

| Path | Contents |
| --- | --- |
| `docker/docker-compose.yml` | Canonical three-service topology, limits, hardening |
| `docker-compose.yml` (root) | `extends` entrypoint for discovery and audit |
| `docker/qdrant/config.yaml` | Storage paths, listeners, payload-on-disk, telemetry off |
| `docker/worker/Dockerfile` | Shared worker/API image, uid 10001 |
| `src/vlake/config.py` | Env + YAML settings, validation |
| `src/vlake/embeddings.py` | `hashing`, `fastembed` (ONNX) and `sentence-transformers` providers |
| `src/vlake/schema.py` | Collection init/inspect CLI (`--recreate`, `--inspect`) |
| `src/vlake/ingestion.py` | Chunking, batching, upsert, CLI |
| `src/vlake/search.py` | FastAPI `/health` `/ready` `/search` |
| `src/vlake/evaluation.py` | recall@k / MRR loop |
| `configs/vector_db.yaml` | Collection shape, HNSW, quantization |
| `configs/embedding_models.yaml` | Providers, dimensions, chunking rationale |
| `configs/datasets/` | Synthetic corpus + labelled queries (tracked) |
| `terraform/` | OCI VCN, A1.Flex instance, block volume, cloud-init (Stage 2) |
| `MANUAL_PREREQS.md` | Human operator checklist: credentials, storage, models, `.env` |
| `scripts/verify_local.py` | Latency/recall harness, RAG context assembly (stdlib only) |
| `tests/test_pipeline.py` | 21 dependency-free unit tests |

## 4. Agent preflight (run before touching anything)

```bash
git status --porcelain                  # must be empty before you start
git log --oneline -3                    # know the baseline
docker version --format '{{.Server.Version}}'
docker compose version
python -m unittest discover -s tests -q
```

If the tree is dirty, stop and report it. Do not "clean up" someone else's work.

## 5. Local bring-up playbook

```bash
cp .env.example .env
mkdir -p storage/qdrant storage/snapshots
docker compose --env-file .env -f docker/docker-compose.yml config -q

# 1. database
docker compose --env-file .env -f docker/docker-compose.yml up -d qdrant
docker inspect -f '{{.State.Health.Status}}' cg-vector-qdrant   # healthy

# 2. ingestion (one-shot; creates the collection on first run)
docker compose --env-file .env -f docker/docker-compose.yml run --rm ingest-worker

# 3. retrieval API
docker compose --env-file .env -f docker/docker-compose.yml up -d search-api
curl -s http://127.0.0.1:8085/health
curl -s http://127.0.0.1:8085/ready
```

Expected: `{"status":"ok",...}` then `{"status":"ready","collection":"rag_documents"}`.
The worker prints a JSON summary; `points_written` must equal `chunks`.

Stop without losing vectors:

```bash
docker compose --env-file .env -f docker/docker-compose.yml down
```

That removes containers and the bridge network only. Delete `storage/` solely
when the corpus is meant to be destroyed.

### Code checks that need no Docker

```bash
python -m unittest discover -s tests -v
PYTHONPATH=src python -m vlake.ingestion --dry-run \
  --dataset configs/datasets/sample_documents.jsonl
python -m compileall -q src tests
```

`--dry-run` loads, validates and chunks the corpus without Qdrant or a model.

## 6. Persistent storage playbook

Two environments, one contract: Qdrant writes to the paths in
`VECTOR_STORAGE_DIR` and `VECTOR_SNAPSHOT_DIR`.

**Workstation (default).** Relative paths from `docker/`:

```bash
VECTOR_STORAGE_DIR=../storage/qdrant
VECTOR_SNAPSHOT_DIR=../storage/snapshots
```

Both are gitignored. Verify:

```bash
du -sh storage/qdrant
ls storage/qdrant/collections
```

**OCI host (Stage 5).** The Terraform block volume is mounted at
`/mnt/vector-lake`. Use absolute paths in `.env`, or the containers will write
to the boot disk and the data will not survive instance replacement:

```bash
VECTOR_STORAGE_DIR=/mnt/vector-lake/storage/qdrant
VECTOR_SNAPSHOT_DIR=/mnt/vector-lake/storage/snapshots
```

Verify the mount before starting containers:

```bash
df -h /mnt/vector-lake
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT
systemctl status vector-lake-mount --no-pager
```

A healthy host shows the volume on `/mnt/vector-lake`, ext4 label `vectorlake`,
and `active (exited)` for the mount unit. If the mount is missing, stop: do not
ingest into a boot-disk directory that will be lost.

## 7. Ingestion playbook

```bash
# plan only
docker compose --env-file .env -f docker/docker-compose.yml run --rm \
  --entrypoint python ingest-worker -m vlake.ingestion \
  --dataset configs/datasets/sample_documents.jsonl --dry-run

# real ingest
docker compose --env-file .env -f docker/docker-compose.yml run --rm ingest-worker

# create the collection explicitly (no-op when it already matches)
docker compose --env-file .env -f docker/docker-compose.yml run --rm \
  --entrypoint python ingest-worker -m vlake.schema

# inspect without writing anything
#   ... --entrypoint python ingest-worker -m vlake.schema --inspect

# rebuild the collection from scratch (DESTRUCTIVE: drops all points)
#   ... --entrypoint python ingest-worker -m vlake.schema --recreate
```

Rules:

- Point IDs are UUIDv5 of `document-id::chunk-index`, so a re-run overwrites
  rather than duplicates. Confirm with `collection_points` staying constant.
- `--recreate` drops the collection. State that intent in the response before
  running it, and never use it on a corpus you did not just rebuild.
- A custom corpus must be JSONL with `id` and `text` per line and no duplicate
  IDs. Put private corpora under `data/` (gitignored), never in
  `configs/datasets/`.
- Chunking is character-based (`CHUNK_SIZE`, `CHUNK_OVERLAP`) with whitespace
  boundaries. Changing it changes point IDs and requires `--recreate` to avoid
  stale chunks for the same document.

## 8. Query and evaluation playbook

```bash
curl -s http://127.0.0.1:8085/search \
  -H 'content-type: application/json' \
  -d '{"query":"how does quantization reduce memory","limit":3}'

# restrict to one document
curl -s http://127.0.0.1:8085/search \
  -H 'content-type: application/json' \
  -d '{"query":"memory","limit":2,"doc_id":"doc-vector-quantization"}'
```

Response contains `hits[]` (`chunk_id`, `doc_id`, `score`, `text`) and
`latency_ms`. Empty query → `422`; Qdrant down → `503`.

```bash
docker compose --env-file .env -f docker/docker-compose.yml run --rm \
  --entrypoint python ingest-worker -m vlake.evaluation \
  --dataset configs/datasets/eval_queries.jsonl --k 5
```

Use the working-directory-relative dataset path: Git Bash on Windows rewrites
`/app/...` into a host path and the container cannot find it.

Score interpretation: with `EMBEDDING_PROVIDER=hashing` the numbers measure
lexical overlap only. Never present them as semantic RAG quality.

Semantic numbers require the ONNX or transformer provider:

```bash
VLAKE_EXTRAS=[semantic] docker compose --env-file .env \
  -f docker/docker-compose.yml build ingest-worker
# .env: EMBEDDING_PROVIDER=fastembed
```

`scripts/verify_local.py` adds service-side and end-to-end latency percentiles,
per-query recall and a grounded RAG prompt. Keep `FASTEMBED_THREADS=1` on a
1-CPU API container; more ONNX threads than the CPU quota causes cgroup
throttling and 100 ms latency steps.

## 9. Monitoring: index health, memory, disk, logs

**Index health**

```bash
curl -s http://127.0.0.1:8085/ready
# collection detail (payload size, points, index status):
docker compose --env-file .env -f docker/docker-compose.yml exec -T search-api \
  python -c "from vlake.config import Settings; from vlake.ingestion import build_client; \
s=Settings.from_env(); c=build_client(s); i=c.get_collection(s.collection); \
print(i.status, c.count(s.collection, exact=True).count, i.config.params.vectors.size)"
```

Expect `green`, a non-zero point count, and the configured dimension. A
`yellow`/`red` status means optimization is behind or a segment is unavailable.

**RAM / CPU / OOM**

```bash
docker stats --no-stream cg-vector-qdrant cg-vector-api
free -h
docker inspect -f '{{.State.OOMKilled}}' cg-vector-qdrant
```

Qdrant is capped by `QDRANT_MEM_LIMIT` (8 GB default) and keeps vectors in RAM
according to the collection config; the worker peaks during embedding batches.
`OOMKilled=true` means the limit is too low for the corpus — raise it or reduce
`INGEST_BATCH_SIZE`, do not disable the limit.

**Logs** (bounded at 20 MiB × 3 files per container)

```bash
docker compose --env-file .env -f docker/docker-compose.yml logs -f qdrant
docker compose --env-file .env -f docker/docker-compose.yml logs --tail=200 search-api
docker compose --env-file .env -f docker/docker-compose.yml logs ingest-worker
docker compose --env-file .env -f docker/docker-compose.yml ps
```

**Error triage:** search-api logs each failed query with the Qdrant error;
Qdrant logs collection creation, optimizer runs and snapshot work. The worker
exits non-zero on a configuration error (2) or runtime failure (1) and prints
the reason on stderr.

**Audit trail:** there is no dedicated audit subsystem. Application events are
container logs with rotation, and stage evidence belongs in
`docs/stage-N-verification.txt`. For production retention, ship stdout to an
external log sink; do not add an unrotated file inside the container.

## 10. Configuration rules and safe-change protocol

- `VECTOR_SIZE` = collection `vector_size` = embedding model dimension. Mismatch
  is a hard error by design.
- Changing `configs/vector_db.yaml` does not migrate an existing collection.
  Apply it with `--recreate` and say so first.
- Changing `EMBEDDING_PROVIDER` or the model also changes the dimension and the
  vector space: recreate and re-ingest, then re-run evaluation. Never mix
  vectors from two models in one collection.
- Qdrant client and server minors must stay within one version. The image pins
  `qdrant/qdrant:v1.12.4` and `qdrant-client>=1.12,<1.13`; bump both together.
- Resource limits live in `.env` and the compose file; do not remove
  `security_opt`, `cap_drop`, `read_only` or log rotation to make a test pass.

## 11. Failure playbook

| Symptom | Cause | Action |
| --- | --- | --- |
| `/ready` → 503 `collection ... is missing` | no ingestion yet | run the ingest worker |
| `/search` → 503 "vector database is unavailable" | Qdrant down or key mismatch | `logs qdrant`, compare `QDRANT_API_KEY` in both services |
| worker exits `configuration error: ...` | bad env value, dimension mismatch, bad JSONL | read stderr, fix `.env`/dataset |
| worker exits 1 during upsert | Qdrant unreachable or OOM | check `docker stats`, raise limit or lower batch |
| `QDRANT_API_KEY is required` from compose | `.env` missing | `cp .env.example .env` |
| mounted volume empty after reboot | fstab/unit failure | `systemctl status vector-lake-mount`, `journalctl -u vector-lake-mount` |
| retrieval looks wrong, no error | hashing provider is lexical | switch to transformer provider, re-ingest, re-evaluate |

## 12. Git and evidence discipline

- Verify the boundary with `git check-ignore -v <path>` and
  `git status --porcelain`; never by eye.
- Never commit `.env`, `terraform.tfvars`, state, `storage/`, keys or logs.
- Commit messages describe why; no attribution trailers.
- A stage is complete only when its measured output is in
  `docs/stage-N-verification.txt` and committed.
- Report commands and their real output. `config -q` proves syntax, not quality;
  a local run proves this machine, not OCI capacity.

## 13. Cloud portability and certification scope

The container configuration and the OpenTofu modules are **cloud-agnostic**.
The same topology runs on AWS (EC2 + EBS + VPC), GCP (GCE + Persistent Disk +
VPC) or OCI (A1.Flex + Block Volume + VCN); only `terraform/provider.tf` and the
three module bodies change, while variables, outputs and the compose contract
stay fixed.

**Certification scope.** Final production verification and testing certification
for this project are performed strictly on **Oracle Cloud Infrastructure**,
using trial credits with high-RAM **Ampere ARM** instances (`VM.Standard.A1.Flex`,
4 OCPU / 24 GB) and persistent **OCI Block Volume** storage. AWS and GCP are
supported ports, not certified targets: no AWS or GCP certification run is made
or implied.

**Status.** The OCI run is Stage 5 and executed 2026-11-18: eight resources
applied in `eu-frankfurt-1`, block volume mounted at `/mnt/vector-lake`, the
stack healthy, and live ingest plus retrieval verified through an SSH tunnel
(`docs/stage-5-verification.txt`). Two constraints keep this short of a
certification claim: A1.Flex was out of capacity in all three availability
domains, so the host is `VM.Standard.A2.Flex` (4 OCPU / 24 GB, AmpereOne), and
the corpus is 10 synthetic documents on a single node with no load, failover or
snapshot drill. Do not describe the project as production-ready, highly
available, or benchmarked at scale; those are Stage 6 and 7.

## 14. Stage roadmap

| Stage | Deliverable | Exit criteria |
| --- | --- | --- |
| 1 | Scaffold, compose, worker/API/eval, configs | unit tests, compose config, local pipeline run — **done** |
| 2 | OCI network, compute, block volume, cloud-init | `fmt`, `validate`, read-only plan — **done** |
| 3 | Agent guide and manual prerequisites | docs complete, links valid, committed — **done** |
| 4 | Local testing: schema, semantic ingest, retrieval, latency | recall@k / latency recorded, no OOM — **done** (`docs/stage-4-verification.txt`) |
| 5 | Live OCI deployment | cloud-init done, volume mounted, healthy services, live ingest/search — **done** (`docs/stage-5-verification.txt`) |
| 6 | Scale and recovery | snapshot/restore drill, load test, monitoring |
| 7 | Security audit, documentation, cross-cloud cost model | zero-leak audit signed off, README with architecture/portability/costs — **done** (`docs/stage-7-security-audit.txt`) |

## 15. Handoff checklist

```bash
git status --porcelain                  # clean
python -m unittest discover -s tests -q # 21 tests
docker compose --env-file .env -f docker/docker-compose.yml config -q
cd terraform && tofu fmt -recursive -check && tofu validate && cd ..
git check-ignore -v .env terraform/terraform.tfvars
```

State what you actually ran, what you did not run, and which stage evidence file
covers it.
