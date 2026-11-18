# OpenTofu — OCI infrastructure (Stage 2)

Infrastructure for the vector lake: a public VCN with a strict security list, a
high-memory Ampere instance, and a persistent block volume attached at a stable
mount point. The container stack from Stage 1 runs unchanged on top.

No resource has been applied yet. Stage 2 delivers the reviewed plan; the live
deployment is Stage 3.

## Resource inventory

| Module | Resources | Purpose |
| --- | --- | --- |
| `network` | VCN, internet gateway, route table, security list, public subnet | One public subnet, explicit routing, minimal inbound surface |
| `compute` | `oci_core_instance` (Ampere A1.Flex, 4 OCPU / 24 GB by default) + primary VNIC lookup | Runs Qdrant, the ingestion worker and the retrieval API |
| `storage` | `oci_core_volume` (200 GB, Balanced) + `oci_core_volume_attachment` | Qdrant segments, WAL, payloads and snapshots survive instance replacement |

`cloud-init.yaml.tftpl` mounts the data volume at `/mnt/vector-lake`, writes an
fstab entry by ext4 label, and creates `storage/qdrant` + `storage/snapshots`
owned by uid 1000. A systemd unit retries the mount on every boot because OCI
can finish attaching the volume after cloud-init's first pass. Cloud-init
installs nothing and contains no credential.

## Ingress contract

| Port | Source | Why |
| --- | --- | --- |
| 22/tcp | `ssh_allowed_cidrs` | Operator or VPN range only |
| 6333/tcp | `vector_api_cidrs` | Qdrant REST |
| 6334/tcp | `vector_api_cidrs` | Qdrant gRPC |
| 19530/tcp | `vector_api_cidrs` | Milvus alternative — set `vector_db_ports = [19530]` |

Both CIDR lists reject `0.0.0.0/0` and `::/0` at plan time. Defaults are RFC 5737
documentation ranges and must be replaced in `terraform.tfvars`. Egress is
explicit: 80/tcp, 443/tcp and 53/udp only.

## Usage

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
chmod 600 terraform.tfvars          # tenancy/compartment OCIDs, key path

tofu init
tofu fmt -recursive -check
tofu validate
tofu plan                            # review: 8 resources to add
# tofu apply                         # Stage 3, with operator approval
```

`tofu apply` is a deployment action. It creates billable/credited resources and
is not part of Stage 2.

## Inputs

| Variable | Default | Notes |
| --- | --- | --- |
| `tenancy_ocid`, `compartment_ocid` | — | Required; real values live only in `terraform.tfvars` |
| `region` | `eu-frankfurt-1` | Any OCI commercial region |
| `vcn_cidr` / `subnet_cidr` | `10.9.0.0/16` / `10.9.1.0/24` | Disjoint from the other portfolio projects |
| `ssh_allowed_cidrs` | placeholder /32 | Operator or VPN range |
| `vector_api_cidrs` | placeholder /32 | Application or operator range |
| `vector_db_ports` | `[6333, 6334]` | Use `[19530]` for a Milvus-compatible port |
| `instance_shape` | `VM.Standard.A1.Flex` | Ampere ARM, Always Free eligible |
| `instance_ocpus` / `instance_memory_gbs` | `4` / `24` | A1 ceiling; raise for `VM.Standard3.Flex` |
| `boot_volume_size_gbs` | `100` | OS, Docker and images only |
| `block_volume_size_gbs` | `200` | Vector segments, WAL, payloads, snapshots |
| `block_volume_vpus_per_gb` | `10` | 0 = Lower Cost, 10 = Balanced, up to 120 |
| `volume_attachment_type` | `paravirtualized` | `iscsi` needs multipathd |
| `volume_mount_point` | `/mnt/vector-lake` | Bind-mount source on the host |
| `ssh_public_key` | — | Public key content, never a private key |
| `availability_domain` | `null` | First AD; pin AD-2/AD-3 when A1 has no capacity |

## Outputs

`instance_public_ip`, `instance_private_ip`, `instance_id`, `instance_shape`,
`availability_domain`, `image_id`, `block_volume_id`,
`block_volume_attachment_id`, `block_volume_size_gbs`,
`block_volume_vpus_per_gb`, `block_volume_device`, `block_volume_mount_point`,
`vcn_id`, `subnet_id`, `security_list_id`, `ingress_ports`,
`ingress_exposure`, `vector_endpoints`, `ssh_connection`, `deploy_next_step`.

`ingress_exposure` is the audit view: every port/source pair that is actually
open. A world-open pair appearing there is a finding, not a configuration.

## State

Local by default and gitignored (`*.tfstate`, `*.tfvars`, `.terraform/`).
`versions.tf` documents OCI Object Storage (S3-compatible), AWS S3 + DynamoDB and
GCS backends for a shared environment. Never commit state: it carries resource
identifiers and provider attributes.

## Portability

| Concern | OCI (implemented) | AWS | GCP |
| --- | --- | --- | --- |
| Network | `oci_core_vcn` + IGW + route table + security list + subnet | `aws_vpc` + IGW + route table + security group + subnet | `google_compute_network` + firewall + subnetwork |
| Compute | `oci_core_instance` A1.Flex | `aws_instance` `t4g.xlarge` | `google_compute_instance` `t2a-standard-4` |
| Block storage | `oci_core_volume` + attachment (VPUs) | `aws_ebs_volume` gp3 + attachment | `google_compute_disk` pd-balanced + attached disk |
| Instance metadata | base64 `user_data` | plain `user_data` | `metadata_startup_script` |

The root variables, outputs and module boundaries do not change on a port; only
`provider.tf` and the three module bodies do. Comments in every file name the
equivalent resource.

## Cost note

The configured footprint is 4 OCPU / 24 GB A1 plus 100 GB boot and 200 GB block
storage. Always Free includes 200 GB of total block storage, so this profile
consumes trial credit for the second 100 GB and for any shape beyond the A1
allowance. Nothing is created until `tofu apply` runs.

## Verification (Stage 2)

Recorded in [`../docs/stage-2-verification.txt`](../docs/stage-2-verification.txt):
`tofu init`, `fmt -check`, `validate` and a read-only `plan` (8 to add, 0 to
change, 0 to destroy) using local gitignored credentials. No apply, no cloud
resource, no drift.
