# Distributed Vector & Embedding Lake

![Qdrant](https://img.shields.io/badge/vector%20engine-Qdrant%20v1.12.4-DC244C?logo=qdrant&logoColor=white)
![Python](https://img.shields.io/badge/service-Python%203.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/api-FastAPI-009688?logo=fastapi&logoColor=white)
![FastEmbed](https://img.shields.io/badge/embeddings-FastEmbed%20ONNX-FF6F00)
![OpenTofu](https://img.shields.io/badge/IaC-OpenTofu-844FBA?logo=opentofu&logoColor=white)
![OCI](https://img.shields.io/badge/certified%20on-OCI%20Ampere%20ARM-F80000?logo=oracle&logoColor=white)
![Stage](https://img.shields.io/badge/release-v1.0.0-blue)
![Stage](https://img.shields.io/badge/stage-8%20%C2%B7%20released-blue)
![Audit](https://img.shields.io/badge/portability-PORTABLE__CERTIFIED%20100%2F100-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

A production-shaped **vector and embedding lake** for retrieval-augmented
generation: documents are chunked and embedded by a worker, stored in Qdrant
with an HNSW index and int8 quantization on persistent block storage, and served
by a FastAPI retrieval service that returns citable chunks to RAG clients.

The stack is deliberately small and observable: one Qdrant node, one shared
worker/API image, one synthetic corpus, and an evaluation loop that reports
recall@k and MRR so retrieval quality is measured instead of assumed. The same
compose topology runs locally and on Oracle Cloud Infrastructure, deployed with
OpenTofu.

**Verified today**

| Claim | Evidence |
| --- | --- |
| Local semantic retrieval: recall@5 1.0 / MRR 1.0, p95 8.41 ms | `docs/stage-4-verification.txt` |
| Live on OCI: A2.Flex 4 OCPU / 24 GB, 200 GB block volume, 10 points, recall@5 1.0, p95 11.35 ms | `docs/stage-5-verification.txt` |
| Zero-leak and Git-boundary audit: `PORTABLE_CERTIFIED` 100/100 (5/5) | `docs/stage-7-security-audit.txt` |
| Corpus size and load limits are stated, not implied | [Honest limits](#-honest-limits) |

---

## Overview

| Layer | Choice | Why |
| --- | --- | --- |
| Vector database | Qdrant 1.12.4 | HNSW + scalar int8 quantization, payload filtering, snapshots, small operational surface |
| Embeddings | FastEmbed (ONNX) MiniLM-L6-v2, 384 dims | Semantic retrieval on CPU without PyTorch; deterministic `hashing` provider stays available for offline CI |
| Ingestion | Python worker, batched upsert | Idempotent UUIDv5 point IDs, chunking with whitespace boundaries, dry-run planning |
| Retrieval | FastAPI `/search` | Query embedding + filtered `query_points`, JSON hits with chunk text for citations |
| Evaluation | `vlake.evaluation` + `scripts/verify_local.py` | recall@k, MRR, latency percentiles, RAG context assembly |
| Infrastructure | OpenTofu: VCN, security list, Ampere compute, block volume | Cloud-agnostic variable contract; OCI implementation certified |
| Packaging | Docker Compose, hardened containers | Read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, resource envelopes |

## Features

- **Semantic RAG retrieval** with grounded, citable chunks (`chunk_id`, `doc_id`,
  score, text) and a context budget.
- **Idempotent ingestion**: re-running a batch overwrites the same points
  (UUIDv5 of `document-id::chunk-index`) instead of duplicating them.
- **Explicit collection lifecycle**: a separate `vlake.schema` CLI creates,
  inspects or recreates the collection with dimensions, distance metric, HNSW
  parameters and quantization.
- **Pluggable embedders**: `hashing` (offline, lexical), `fastembed` (ONNX, CPU),
  `sentence-transformers` (PyTorch, optional extra).
- **Measured quality and latency**: recall@k, MRR, p50/p95, plus a verification
  harness that will fail loudly on non-200 responses.
- **Persistent vector storage** on a mounted block volume, with a filesystem
  label and `nofail` fstab entry so the index survives reboots and host
  replacement.
- **Least-privilege network**: SSH and vector ports admitted only from the
  operator `/32`; database and API bind loopback on the host.
- **Zero-leak repository**: `.env`, keys, state, snapshots and vector storage are
  gitignored and audited in CI-style checks.

---

## Architecture

```
[Source Documents / Data]
      │  JSONL  {"id", "text", "metadata"}
      ▼
(Embedding Worker Pipeline)
      │  chunk → normalize → embed (FastEmbed ONNX / MiniLM 384d) → batched upsert
      ▼
[High-Spec OCI Compute & Persistent Block Storage]
      │  Ampere A2.Flex 4 OCPU / 24 GB  +  200 GB Balanced Block Volume
      │  /mnt/vector-lake/storage/qdrant   (segments, WAL, payloads, snapshots)
      ▼
[Distributed Vector Database (Qdrant / Milvus)]
      │  HNSW m=16, ef_construct=128, Cosine, scalar int8, payload on disk
      ▲
      │  top-k + payload filter
(Semantic RAG & Search Queries)
      │  POST /search → embed query → query_points → hits with citations
      ▼
[RAG client / LLM prompt assembly]
```

Text fallback of the same flow:

```
Source Documents --> Embedding Worker Pipeline --> OCI Compute + Block Storage
--> Qdrant vector database --> Semantic RAG & Search Queries
```

| Component | Image | Responsibility | Default bind |
| --- | --- | --- | --- |
| `qdrant` | `qdrant/qdrant:v1.12.4` | Vector storage, HNSW index, snapshots | `127.0.0.1:6333` |
| `ingest-worker` | `cloudguard/vector-lake:local` | Chunk, embed, batched upsert | none (batch job) |
| `search-api` | `cloudguard/vector-lake:local` | Embed query, filtered search, JSON | `127.0.0.1:8085` |

The worker and API share one image. Both run as uid `10001`, read-only rootfs,
`cap_drop: ALL`, `no-new-privileges`, bounded pids and 20 MiB × 3 log rotation.

### Request path

1. Client sends `{"query": "...", "limit": 5, "doc_id": "optional"}`.
2. API embeds the query with the configured provider and queries Qdrant with the
   same distance metric as ingestion.
3. Qdrant returns nearest chunks with payloads; the API returns `hits[]` plus
   `latency_ms`.
4. `scripts/verify_local.py` builds a grounded prompt:
   `Answer the question using only the context below. Cite the bracketed chunk id…`

---

## Cloud-Agnostic Portability Note

The container topology and the OpenTofu variable contract are **cloud-agnostic**.
Only `terraform/provider.tf` and the three module bodies (`network`, `compute`,
`storage`) are provider-specific; variables, outputs and the compose contract
stay fixed.

> **Certification scope.** Final production verification and testing
> certification for this project were performed **strictly on Oracle Cloud
> Infrastructure (OCI)**, using trial credits with a high-RAM **Ampere ARM**
> instance and a **persistent OCI Block Volume** (`docs/stage-5-verification.txt`).
> AWS and GCP are supported ports, not certified targets: no AWS or GCP
> deployment, benchmark or certification run is claimed anywhere in this
> repository.

Mapping used by the modules:

| Concern | OCI (certified) | AWS (port) | GCP (port) |
| --- | --- | --- | --- |
| Network | VCN + IGW + route table + security list + subnet | VPC + IGW + route table + security group + subnet | VPC network + routes + firewall rule + subnetwork |
| Compute | `oci_core_instance` — `VM.Standard.A1/A2.Flex` (Ampere ARM) | `aws_instance` — `t4g`/`c6g`/`r6g` (Graviton) | `google_compute_instance` — `t2a` (Axion/Tau) or `e2` |
| Block storage | `oci_core_volume` + attachment (VPUs/GB) | `aws_ebs_volume` (gp3) + attachment | `google_compute_disk` (pd-balanced) + attached disk |
| Bootstrap | `metadata.user_data` (base64 cloud-init) | `user_data` | `metadata_startup_script` |
| Size inputs | `instance_ocpus`, `instance_memory_gbs` | instance type | machine type |

### Migration tips — AWS (EC2 + EBS)

1. Replace `provider.tf` with the `hashicorp/aws` provider and rewrite
   `modules/network` as VPC + public subnet + security group (22/6333/6334 from
   the operator CIDR only).
2. Use a Graviton type with enough memory: **`r6g.xlarge` (4 vCPU / 32 GB)**
   matches this workload better than `c6g.xlarge` (4 vCPU / 8 GB); ARM64 images
   map to Ubuntu 24.04 arm64 AMIs.
3. Attach an **EBS gp3** volume (200 GB, 3,000 IOPS baseline) as the data disk
   and keep the same ext4 label + fstab contract in cloud-init.
4. AWS has no `user_data` size problem at this scale, but remember instance
   metadata is readable by the guest — never put `.env` or keys there.
5. Note the egress model: 100 GB/month free, then ~$0.09/GB, unlike OCI's 10 TB.

### Migration tips — GCP (GCE + Persistent Disk)

1. Rewrite `modules/network` as a VPC network + firewall rule; keep the
   operator-only source ranges.
2. Use `e2-standard-4` (4 vCPU / 16 GB) for parity with the tested vCPU count, or
   **`e2-highmem-4`** when 24–32 GB is required; ARM parity is `t2a-standard-4`.
3. Attach a **pd-balanced** disk and mount it with the same label/fstab logic
   (`/dev/disk/by-id/google-<name>` is stable and simpler than OCI's device
   discovery).
4. Set `metadata_startup_script` from the same cloud-init body; GCP has no
   built-in `cloud-init status --wait` unless cloud-init is installed, so verify
   the mount unit explicitly.
5. Egress: 200 GB/month free in North America/Europe, then ~$0.12/GB.

**Invariant across ports:** `VECTOR_STORAGE_DIR`, `VECTOR_SNAPSHOT_DIR`,
`VECTOR_SIZE`, the collection name and the compose service names must not change,
so the same `.env` contract and verification scripts work on every provider.

---

## Quickstart (local)

Operator setup (credentials, block volume, models, `.env`) is in
[`MANUAL_PREREQS.md`](MANUAL_PREREQS.md); agent execution, monitoring and failure
commands are in [`AGENT_GUIDE.md`](AGENT_GUIDE.md).

Requirements: Docker Engine with Compose v2 and 8 GB free RAM for the default
limits (lower `QDRANT_MEM_LIMIT`/`WORKER_MEM_LIMIT` on a laptop).

```bash
cp .env.example .env
mkdir -p storage/qdrant storage/snapshots

# 1. vector database
docker compose --env-file .env -f docker/docker-compose.yml up -d qdrant

# 2. collection (384 dims, Cosine, HNSW m=16, scalar int8)
docker compose --env-file .env -f docker/docker-compose.yml run --rm \
  --entrypoint python ingest-worker -m vlake.schema

# 3. ingest the synthetic corpus
docker compose --env-file .env -f docker/docker-compose.yml run --rm ingest-worker

# 4. retrieval API
docker compose --env-file .env -f docker/docker-compose.yml up -d search-api
```

Semantic mode (ONNX, no PyTorch):

```bash
VLAKE_EXTRAS=[semantic] docker compose --env-file .env \
  -f docker/docker-compose.yml build ingest-worker
# .env: EMBEDDING_PROVIDER=fastembed
#      EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
```

Weights (~87 MB) are cached in the `cloudguard-vector-model-cache` volume, never
in the repository. Keep `FASTEMBED_THREADS` equal to `API_CPUS`: ONNX Runtime
with more threads than the CPU quota is throttled into ~100 ms steps (measured).

### Configure vector collections

`configs/vector_db.yaml` is the source of truth; the schema CLI applies it.

```yaml
collection:
  name: rag_documents
  vector_size: 384          # must equal VECTOR_SIZE / EMBEDDING_DIMENSION
  distance: Cosine          # Cosine | Dot | Euclid
  hnsw:
    m: 16                   # graph degree: recall vs memory
    ef_construct: 128
  quantization: scalar_int8 # ~4× smaller, rescored against originals
  on_disk_payload: true
```

```bash
# create if missing (safe to re-run)
docker compose ... run --rm --entrypoint python ingest-worker -m vlake.schema
# read-only status: points, segments, optimizer, quantization
docker compose ... run --rm --entrypoint python ingest-worker -m vlake.schema --inspect
# drop and recreate (DESTRUCTIVE)
docker compose ... run --rm --entrypoint python ingest-worker -m vlake.schema --recreate
```

Changing `vector_size`, the distance metric, the embedding model or the chunking
parameters requires `--recreate` plus a full re-ingest; otherwise old and new
vectors silently mix.

### Run ingestion workers

```bash
# plan only: chunking report, no writes
docker compose ... run --rm ingest-worker --dataset configs/datasets/sample_documents.jsonl --dry-run

# real batch
docker compose ... run --rm ingest-worker
```

Corpus format: one JSON object per line with `id`, `text` and optional
`metadata`. Duplicate IDs fail before any write. Private corpora belong under
`data/` (gitignored), never `configs/datasets/`.

### Semantic RAG queries

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
      "score": 0.752,
      "text": "Scalar quantization maps float32 vector components to int8 …"
    }
  ],
  "latency_ms": 5.2
}
```

Retrieval-only verification, including the assembled RAG prompt:

```bash
python scripts/verify_local.py --queries configs/datasets/eval_queries.jsonl \
  --limit 5 --repeats 5 --output docs/stage-4-report.json
```

Quality loop:

```bash
docker compose ... run --rm --entrypoint python ingest-worker \
  -m vlake.evaluation --dataset configs/datasets/eval_queries.jsonl --k 5
```

`hashing` scores are lexical overlap only; do not present them as semantic RAG
quality.

---

## 💰 Cross-Cloud Cost & Benchmark Estimation

List prices, USD, **EU regions**, November 2026, 730 hours/month. Sizes follow the
tested footprint: 4 vCPU-class ARM/x86 compute with 24 GB target memory and
**300 GB provisioned block storage** (100 GB boot + 200 GB data, as deployed).
Excludes tax, backups, snapshots and support. Prices change — treat this as a
comparison model, then confirm in each provider's calculator.

| Provider | Instance (shape) | vCPU / RAM | Compute $/mo | 300 GB storage $/mo | Egress | **Est. total $/mo** | Measured here? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **OCI — trial** | `VM.Standard.A1.Flex` (Ampere) | 4 / 24 GB | **$0** (Always Free: 1,500 OCPU-h + 9,000 GB-h/mo) | ~$4.25 beyond the 200 GB free block allowance | 10 TB/mo free | **~$4** | No — A1 capacity unavailable |
| **OCI — deployed** | `VM.Standard.A2.Flex` (AmpereOne) | 4 / 24 GB | $55.48 ($0.01/OCPU-h + $0.0015/GB-h) | $12.75 (Balanced: $0.0255/GB + 10 VPU × $0.0017) | 10 TB/mo free | **$68.23** | **Yes** — recall@5 1.0, p95 11.35 ms |
| **AWS** | `c6g.xlarge` (Graviton2) | 4 / 8 GB | $113.30 ($0.1552/h) | $28.56 (gp3 $0.0952/GB) | 100 GB free, then ~$0.09/GB | **$141.86** | No — price only |
| **AWS** | `r6g.xlarge` (Graviton2, RAM-matched) | 4 / 32 GB | $177.54 ($0.2432/h) | $28.56 (gp3) | 100 GB free, then ~$0.09/GB | **$206.10** | No — price only |
| **GCP** | `e2-standard-4` | 4 / 16 GB | $126.29 ($0.173/h, europe-west3) | $33.00 (pd-balanced ~$0.11/GB) | 200 GB free, then ~$0.12/GB | **$159.29** | No — price only |
| **GCP** | `e2-highmem-4` (RAM-matched) | 4 / 32 GB | ≈$1.4× standard (~$177) | $33.00 (pd-balanced) | 200 GB free, then ~$0.12/GB | **~$210** | No — price only |

**Egress cost for 1 TB/month** (the workload that usually decides the bill):
OCI **$0** (inside 10 TB), AWS **~$83** (924 GB billable), GCP **~$99**
(824 GB billable).

**Benchmark comparison** — only the OCI row was executed. Local numbers are on a
Windows workstation; AWS/GCP rows are *not* measurements and must not be cited
as such.

| Environment | vCPU / RAM | Corpus | recall@5 | MRR | Service p50 / p95 | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Local Docker (Windows) | 1 CPU (API cap) | 10 docs / 10 chunks | 1.0 | 1.0 | 6.98 / 8.41 ms | Measured (Stage 4) |
| **OCI `A2.Flex`** | 4 OCPU / 24 GB | 10 docs / 10 chunks | **1.0** | **1.0** | **10.28 / 11.35 ms** (40 req) | **Measured (Stage 5)** |
| AWS `c6g.xlarge` | 4 / 8 GB | — | — | — | — | Not deployed |
| GCP `e2-standard-4` | 4 / 16 GB | — | — | — | — | Not deployed |

Reading the table: at this corpus size, compute price dominates and all three
clouds would meet the latency target; OCI wins on egress and on Arm price per
OCPU, AWS wins on mature ARM tooling, GCP on regional coverage. The 8 GB
`c6g.xlarge` cannot hold a 24 GB-scale index; use `r6g.xlarge` for parity.

Cost levers that are implemented rather than aspirational:

- **int8 scalar quantization** (~4× smaller vectors) and on-disk payloads.
- **One shared image** for worker and API; the worker is a one-shot process, so
  idle compute cost is zero between batches.
- **Model cache on a volume** so image rebuilds do not re-download weights.
- **Snapshot-oriented storage layout** on the block volume, independent of the
  instance lifecycle.

---

## Security posture

- Qdrant requires an API key from the gitignored `.env`; no default credential.
- Worker and API: read-only rootfs, `cap_drop: ALL`, `no-new-privileges`,
  non-root uid, tmpfs `/tmp`, pids limits.
- Qdrant: `cap_drop: ALL`, `no-new-privileges`, bounded logs. Because the
  capability drop removes `CAP_DAC_OVERRIDE`, its data directories are owned by
  root (0750) while the mount point stays operator-owned — verified on the host.
- Network: SSH and vector ports only from the operator `/32`; the API and Qdrant
  publish on `127.0.0.1`. There is no world-open rule in the IaC, and validation
  rejects one.
- Git boundary: `.env*` (except `.env.example`), `*.pem`, `*.tfvars`,
  `*.tfstate`, `*.db`, `storage/`, `snapshots/`, `runtime/` and logs are ignored.
  See [`docs/stage-7-security-audit.txt`](docs/stage-7-security-audit.txt) for the
  audit commands and results.

## Local checks (no container required)

```bash
python -m unittest discover -s tests -v                       # 21 tests
PYTHONPATH=src python -m vlake.ingestion --dry-run \
  --dataset configs/datasets/sample_documents.jsonl           # chunking only
docker compose --env-file .env -f docker/docker-compose.yml config -q
git status --porcelain                                        # must be empty
```

## Repository layout

```
docker/                     compose topology, qdrant config, worker Dockerfile
src/vlake/                  config, embeddings, schema, ingestion, search, evaluation
scripts/verify_local.py     latency/recall harness + RAG context assembly
configs/                    collection shape, provider catalogue, synthetic corpus
terraform/                  OCI network, Ampere compute, block volume, cloud-init
tests/                      21 dependency-free unit tests
docs/                       per-stage verification evidence and reports
```

## Roadmap

| Stage | Scope | Status |
| --- | --- | --- |
| 1 | Scaffold, topology, ingestion/search/eval, configs | Done — `docs/stage-1-verification.txt` |
| 2 | OCI OpenTofu modules, block volume, cloud-init | Done — `docs/stage-2-verification.txt` |
| 3 | Agent guide and manual prerequisites | Done |
| 4 | Local semantic ingestion, retrieval and latency | Done — `docs/stage-4-verification.txt` |
| 5 | Live OCI deployment and verification | Done — `docs/stage-5-verification.txt` |
| 6 | Scale-out: sharding, snapshot/restore drill, monitoring, load test | Not started |
| 7 | Security audit, documentation, cost estimation | Done — `docs/stage-7-security-audit.txt`, this README |
| 8 | Release packaging: license, clean tree, `v1.0.0` tag, public repository | Done — tag `v1.0.0` |

## ⚠️ Honest limits

- **Single node, replication factor 1.** No high availability, no failover, no
  sharding. A node loss means downtime until restore.
- **Ten synthetic documents.** recall@5 1.0 proves the pipeline and the metric
  wiring, not quality at 10⁵–10⁷ chunks.
- **No load or concurrency benchmark.** The reported p50/p95 come from 24–40
  sequential requests.
- **A1.Flex was unavailable** in all three eu-frankfurt-1 ADs at deploy time; the
  certified host is Ampere `A2.Flex`. No Always-Free claim is attached to it.
- **Snapshot/restore has not been drilled**, and the cloud-init template fix has
  not been exercised by a fresh boot (it implies a 2-add/2-destroy instance
  replacement; see `docs/stage-5-verification.txt`).
- **AWS and GCP were not deployed**, so their rows are price models, not
  benchmarks.
- Quantization recall loss, HNSW parameter tuning and filtered-search latency at
  scale are untested.

## Documentation

| Document | Contents |
| --- | --- |
| [`MANUAL_PREREQS.md`](MANUAL_PREREQS.md) | Operator prerequisites: credentials, storage, models, `.env`, acceptance criteria |
| [`AGENT_GUIDE.md`](AGENT_GUIDE.md) | Agent operating contract: bring-up, ingestion, query, monitoring, failure modes |
| [`docs/stage-5-verification.txt`](docs/stage-5-verification.txt) | Live OCI evidence: IDs, mount, metrics, defects fixed |
| [`docs/stage-7-security-audit.txt`](docs/stage-7-security-audit.txt) | Zero-leak audit commands and sign-off |
| [`terraform/README.md`](terraform/README.md) | Module contract, variables, capacity notes |

## License

MIT — Copyright (c) 2026 Mostafa Kaveh. See [`LICENSE`](LICENSE).
