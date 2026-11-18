# Manual Prerequisites — Distributed Vector & Embedding Lake

Checklist for the human operator **before** any deployment script runs. Complete
every section in order; each ends with a verification command. If a check fails,
stop and fix it — do not let an agent improvise around a missing prerequisite.

> **Status.** No OCI resource exists yet. The infrastructure is validated
> (`tofu validate`, plan: 8 to add) and the container stack is verified locally.
> The live run is Stage 4 and needs the items below.

## Cloud portability and certification scope

The OpenTofu modules and container configuration are **cloud-agnostic**: the same
topology maps to AWS (EC2 + EBS + VPC), GCP (GCE + Persistent Disk + VPC) and
OCI (A1.Flex + Block Volume + VCN), with only `terraform/provider.tf` and the
three module bodies changing.

Final production verification and testing certification for this project are
performed strictly on **Oracle Cloud Infrastructure**, using trial credits with
high-RAM **Ampere ARM** instances (`VM.Standard.A1.Flex`, 4 OCPU / 24 GB) and
persistent **OCI Block Volume** storage. AWS and GCP are supported ports, not
certified targets; no AWS or GCP certification run is claimed.

This checklist prepares that OCI run. Until Stage 4 records its evidence, the
project is not deployed and not certified on OCI.

---

## 1. What will be created

| Resource | Configuration | Notes |
| --- | --- | --- |
| VCN + internet gateway + route table | `10.9.0.0/16` | Dedicated, not the tenancy default |
| Security list | 22, 6333, 6334 from your CIDRs only | No world-open rule is possible; plan rejects it |
| Public subnet | `10.9.1.0/24` | Single subnet, public IP on the instance |
| Compute instance | `VM.Standard.A1.Flex`, 4 OCPU / 24 GB | Ubuntu 24.04 aarch64 |
| Boot volume | 100 GB | OS, Docker, images |
| Block volume + attachment | 200 GB, Balanced (10 VPUs/GB) | Qdrant segments, WAL, snapshots |

**Cost:** Always Free includes 4 OCPU / 24 GB of A1 and 200 GB of total block
storage. This profile uses 100 GB boot + 200 GB data = 300 GB, so it consumes
trial credit. Nothing is created until `tofu apply`.

---

## 2. Account, compartment and permissions

- [ ] An OCI tenancy with trial credit or a paid account.
- [ ] A dedicated compartment for this project (recommended: `vector-lake`).
- [ ] An IAM policy granting the deploying group the resources below.

```hcl
# Replace <group-name>, <compartment-name> and the tenancy name.
Allow group <group-name> to manage virtual-network-family in compartment <compartment-name>
Allow group <group-name> to manage instance-family in compartment <compartment-name>
Allow group <group-name> to manage volumes in compartment <compartment-name>
Allow group <group-name> to manage volume-attachments in compartment <compartment-name>
Allow group <group-name> to read instances in compartment <compartment-name>
Allow group <group-name> to read vcns in compartment <compartment-name>
Allow group <group-name> to read subnets in compartment <compartment-name>
Allow group <group-name> to read security-lists in compartment <compartment-name>
Allow group <group-name> to read route-tables in compartment <compartment-name>
Allow group <group-name> to read internet-gateways in compartment <compartment-name>
Allow group <group-name> to read availability-domains in tenancy
Allow group <group-name> to read app-catalog-listing in compartment <compartment-name>
```

Read access to the network children is needed because OpenTofu refreshes them
after apply. If your tenancy uses a stricter policy set, run a plan first and
add only what the error names.

- [ ] Copy the **compartment OCID** and **tenancy OCID** somewhere private. They
      go into `terraform/terraform.tfvars`, which is gitignored.

**Verify:** the compartment appears in the console and you can list instances in
it (empty is expected).

---

## 3. Workstation tooling

| Tool | Minimum | Check |
| --- | --- | --- |
| OpenTofu | 1.6 | `tofu version` |
| Docker Engine | 24 | `docker version --format '{{.Server.Version}}'` |
| Docker Compose | v2 | `docker compose version` |
| Git | 2.30 | `git --version` |
| Python (optional, host checks) | 3.11 | `python --version` |

- [ ] All five commands succeed.
- [ ] Docker can run a container: `docker run --rm hello-world`.

On Windows, use Git Bash for the commands in this repository. Git Bash rewrites
`/absolute/container/paths`; the guide uses relative container paths for that
reason.

---

## 4. OCI API credentials

OpenTofu authenticates with an OCI API signing key. The private key never enters
this repository, the plan output or the state file.

1. Console → **Identity → Users → your user → API keys → Add API key**.
2. Choose *Generate API key pair*, download the private key, note the
   **fingerprint** shown.
3. Store the key outside the repo, for example `~/.oci/oci_api_key.pem`, and
   restrict it:

```bash
mkdir -p ~/.oci
mv ~/Downloads/<downloaded-key>.pem ~/.oci/oci_api_key.pem
chmod 600 ~/.oci/oci_api_key.pem
```

4. Create or edit `~/.oci/config` (placeholders only — fill with your values):

```ini
[DEFAULT]
user=<user-ocid>
fingerprint=<api-key-fingerprint>
tenancy=<tenancy-ocid>
region=eu-frankfurt-1
key_file=~/.oci/oci_api_key.pem
```

- [ ] `chmod 600 ~/.oci/config ~/.oci/oci_api_key.pem`
- [ ] No copy of the key or config exists inside the repository:
      `git check-ignore -v .env terraform/terraform.tfvars` and
      `git status --porcelain` stay clean after every step.

**Verify:**

```bash
cd terraform
tofu init
tofu plan            # should reach "Plan: 8 to add" using the DEFAULT profile
```

If the provider reports an authentication error, the fingerprint or key path is
wrong. Fix the config; do not paste key material into chat, a commit, an issue or
a document.

**CI variant:** instead of `~/.oci/config`, export the inline fields OpenTofu
expects (`TF_VAR_user_ocid`, `TF_VAR_fingerprint`, `TF_VAR_private_key_path`)
from your secret store. The key must still live outside the checkout.

---

## 5. SSH key pair

```bash
ssh-keygen -t ed25519 -f ~/.ssh/vector-lake -C "vector-lake"
chmod 600 ~/.ssh/vector-lake
cat ~/.ssh/vector-lake.pub
```

- [ ] Public key content pasted into `terraform/terraform.tfvars` as
      `ssh_public_key`. It must start with `ssh-ed25519 `.
- [ ] The **private** key stays at `~/.ssh/vector-lake` and is never copied into
      the repository.

**Verify:** `ssh-keygen -y -f ~/.ssh/vector-lake` prints the same public key.

---

## 6. Network inputs

- [ ] Your operator public IP:

```bash
curl -s https://ifconfig.me
```

Use it as a `/32`, for example `ssh_allowed_cidrs = ["198.51.100.7/32"]`
(replace with your real address; the value here is documentation-only).

- [ ] Decide `vector_api_cidrs`. Preferred order: the application subnet CIDR,
      then a VPN range, then your operator `/32` for a short verification window.
- [ ] Ports: `[6333, 6334]` for Qdrant. For a Milvus-compatible deployment use
      `[19530]`. List only ports that will actually listen; never add
      `0.0.0.0/0` (plan-time validation rejects it).

| Port | Service | Who should reach it |
| --- | --- | --- |
| 22 | SSH | You / your VPN only |
| 6333 | Qdrant REST | Application or operator |
| 6334 | Qdrant gRPC | Application or operator |
| 19530 | Milvus gRPC (alternative) | Application only, if used |

**Verify:** a plan with your real CIDRs opens exactly the ports you listed
(`tofu plan` output and the `ingress_exposure` output after apply).

---

## 7. Terraform variables

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
chmod 600 terraform.tfvars
```

Fill in:

| Variable | Required | Guidance |
| --- | --- | --- |
| `tenancy_ocid`, `compartment_ocid` | yes | From section 2 |
| `region` | yes | Must match your `~/.oci/config` region |
| `ssh_allowed_cidrs`, `vector_api_cidrs` | yes | From section 6 |
| `ssh_public_key` | yes | Public key content from section 5 |
| `vector_db_ports` | no | `[6333, 6334]` (Qdrant) or `[19530]` (Milvus) |
| `instance_shape`, `instance_ocpus`, `instance_memory_gbs` | no | A1 4/24 default; raise only with credit |
| `block_volume_size_gbs` | no | 200 GB default; see the cost note in section 1 |
| `volume_mount_point` | no | `/mnt/vector-lake` |
| `availability_domain` | no | Pin AD-2/AD-3 if A1 has no capacity in AD-1 |
| `image_id` | no | Pin for a reproducible rebuild; null resolves newest Ubuntu |

- [ ] `tofu fmt -recursive -check` passes (it also formats your local tfvars).
- [ ] `tofu validate` passes.
- [ ] `tofu plan` reports 8 resources to add and no replace or destroy.
- [ ] `git status --porcelain` does not list `terraform.tfvars`.

---

## 8. Persistent block volume

The IaC creates and attaches the volume; cloud-init formats it **only if it has
no filesystem**, mounts it by ext4 label `vectorlake`, adds an `nofail` fstab
entry and creates the data layout.

Expected on the host after Stage 4 apply:

```bash
df -h /mnt/vector-lake
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT
systemctl status vector-lake-mount --no-pager
ls -la /mnt/vector-lake/storage
```

- [ ] `/mnt/vector-lake` is mounted with label `vectorlake`.
- [ ] `storage/qdrant` and `storage/snapshots` exist and are owned by uid 1000.
- [ ] The mount unit is `active (exited)`.
- [ ] A reboot keeps the mount (fstab `nofail`) — verify during Stage 4.

**Data safety:** `tofu destroy` deletes the volume. Before any real corpus lands,
set `lifecycle { prevent_destroy = true }` on `oci_core_volume.vector` (documented
in `terraform/modules/storage/main.tf`) and take a snapshot before teardown.

---

## 9. Embedding models and keys

Pick one provider before starting; the collection dimension must match it.

| Option | Key/weights | Dimension | Use |
| --- | --- | --- | --- |
| `hashing` (default) | none | 384 | Offline verification, CI. Lexical only. |
| `sentence-transformers` | model weights (~90 MB–2.2 GB) | model-specific | Real semantic retrieval |
| Hosted embedding API | provider API key | provider-specific | Not integrated in this repository |

**Local weights (recommended for certification):**

```bash
# on the host or workstation, outside the repo
pip install 'sentence-transformers>=3.2,<4'
python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```

- [ ] The model downloads once and is cached (`HF_HOME`, default
  `~/.cache/huggingface`).
- [ ] `EMBEDDING_MODEL` points at the cached model id, and
  `EMBEDDING_DIMENSION` / `VECTOR_SIZE` match it (MiniLM = 384, bge-m3 = 1024).
- [ ] For an air-gapped host, pre-download the weights, mount the cache into the
  container and set `HF_HUB_OFFLINE=1`.
- [ ] The current image installs base dependencies only; a transformer run needs
  the `models` extra added to the worker image (a one-line Dockerfile change) or
  a host-side virtual environment. Do not assume the container has it.

**Hosted API keys:** this repository has no hosted-embedding client. If you add
one, keep the key server-side in `.env`, never in `configs/`, a browser bundle or
a commit, and never log it. Never paste a key into chat or a document.

**Qdrant key:** generate one per environment; do not reuse the template value.

```bash
openssl rand -hex 24        # paste into .env as QDRANT_API_KEY
```

---

## 10. Populate `.env` securely

```bash
umask 077
cp .env.example .env
chmod 600 .env
```

Non-negotiable values for the OCI host — absolute mount paths, or the data lands
on the boot disk:

```bash
VECTOR_STORAGE_DIR=/mnt/vector-lake/storage/qdrant
VECTOR_SNAPSHOT_DIR=/mnt/vector-lake/storage/snapshots
QDRANT_API_KEY=<generated-value>
API_PORT=8085
```

- [ ] `QDRANT_API_KEY` is generated, unique and not the example value.
- [ ] Storage paths are absolute and point at `/mnt/vector-lake`.
- [ ] `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` and
      `VECTOR_SIZE` agree.
- [ ] `chmod 600 .env`; never `cat` it into a transcript, screenshot or ticket.
- [ ] `git check-ignore -v .env` names `.gitignore`; `git status` stays clean.

Compose refuses to start without `QDRANT_API_KEY`, so a missing key fails loudly
instead of running an unauthenticated database.

---

## 11. Preflight checklist (run all of it)

```bash
# repository
git status --porcelain                       # empty
python -m unittest discover -s tests -q      # 20 tests pass

# container topology
docker compose --env-file .env -f docker/docker-compose.yml config -q

# infrastructure
cd terraform
tofu fmt -recursive -check
tofu validate
tofu plan                                    # 8 to add, 0 to change, 0 to destroy
cd ..

# host resources (after apply)
df -h /mnt/vector-lake
free -h
```

- [ ] Tree clean, tests pass, compose validates.
- [ ] `tofu plan` has no destroy/replace step you did not expect.
- [ ] At least 3 GB free on the boot volume for images and one transformer model.
- [ ] Ports 8085, 6333 and 6334 are free on the host.
- [ ] `.env` exists, is mode 600 and gitignored.

---

## 12. Deployment sequence (Stage 4, operator-approved)

Do not run this without an explicit go-ahead. It creates cloud resources.

```bash
cd terraform
tofu plan -out=stage4.tfplan        # review: 8 to add
tofu apply stage4.tfplan            # approved action

tofu output instance_public_ip
ssh ubuntu@<public-ip>
cloud-init status --wait            # done
df -h /mnt/vector-lake              # volume mounted
free -h                             # ~24 GB total

git clone https://github.com/kaveh8866/-Distributed-Vector-Embedding-Lake.git vector-lake
cd vector-lake
cp .env.example .env && chmod 600 .env    # absolute mount paths + generated key
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose --env-file .env -f docker/docker-compose.yml run --rm ingest-worker
curl -s http://127.0.0.1:8085/ready
```

- [ ] `cloud-init status --wait` returns `done`.
- [ ] The block volume is mounted at `/mnt/vector-lake` with label `vectorlake`.
- [ ] Containers are healthy and the API answers `/ready`.
- [ ] Ingestion summary shows `points_written == chunks`.
- [ ] A search returns a hit with a payload.

---

## 13. Acceptance criteria for Stage 4

| Criterion | Evidence |
| --- | --- |
| Infrastructure applied | apply output: 8 added, 0 destroyed |
| Host bootstrap | `cloud-init status --wait` → `done` |
| Persistent storage | `df -h` shows the volume; survives a reboot |
| Container health | `qdrant` and `search-api` healthy |
| Ingestion | JSON summary with point count |
| Retrieval | `POST /search` 200 with hits |
| Evaluation | recall@k / MRR recorded in `docs/stage-4-verification.txt` |
| Boundary | no key, state, tfvars or corpus tracked |

---

## 14. Teardown and data preservation

1. Take a Qdrant snapshot and copy it somewhere outside the instance.
2. Confirm the snapshot restores into a scratch collection (Stage 5 drill).
3. `tofu plan -destroy` → review → `tofu destroy`.
4. `tofu state list` must be empty.
5. Provider-side check: no instances or VCNs remain in the compartment.
6. Keep `docs/` evidence; remove local state, tfvars and `.env` only when the
   corpus does not need to be revived.

Do not delete the block volume before the snapshot is verified: destroy is not
recoverable.

---

## 15. Stop conditions

Report the blocker instead of improvising when:

- A1 reports **out of host capacity** — pin another AD (`availability_domain`)
  and retry; never substitute a paid shape or a different region silently.
- The plan shows an unexpected **destroy or replace**.
- Credentials are missing or the fingerprint does not match.
- The block volume does not mount after cloud-init.
- A quota or limit error appears — quote the exact error code.
- You are asked to commit `.env`, `terraform.tfvars`, state or keys.

---

## 16. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Provider auth error | wrong fingerprint, key path or profile | recheck `~/.oci/config`, `chmod 600` |
| `NotAuthorizedOrNotFound` on plan | missing IAM policy in the compartment | add the policy from section 2 |
| `Out of host capacity` | A1 capacity in that AD | set `availability_domain` to AD-2/AD-3 |
| `QDRANT_API_KEY is required` | `.env` missing during compose | `cp .env.example .env`, set the key |
| Volume mounted but empty after reboot | fstab/unit failure | `journalctl -u vector-lake-mount`, check label |
| API `/ready` 503 | no ingestion yet | run the ingest worker |
| Search results look irrelevant | hashing provider is lexical | switch to transformer provider and re-ingest |

---

## 17. Certification note

The certified target is OCI only, with Ampere ARM compute and a persistent block
volume, funded by the trial credit allocation. Re-running this checklist on AWS
or GCP produces a supported port, not a certified one. Certification evidence is
recorded in `docs/stage-4-verification.txt` once the live run happens; before
that, no deployment or certification claim is valid.
