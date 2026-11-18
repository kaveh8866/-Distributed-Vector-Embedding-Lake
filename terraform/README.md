# OpenTofu — OCI infrastructure (reserved for Stage 2)

This directory is intentionally free of `.tf` files at Stage 1. It records the
contract the infrastructure must satisfy so the container stack never needs to
change when the lake moves from a laptop to OCI.

## Planned resources

| Resource | Purpose | Notes |
| --- | --- | --- |
| VCN + public subnet | Single-region network | No ingress except the operator CIDR |
| Internet gateway + route table | Egress for image pulls and model downloads | Explicit, no NAT gateway |
| Network security list | TCP **8080** from the operator CIDR only | Qdrant ports 6333/6334 stay private |
| Compute instance | `VM.Standard.A1.Flex` (Always Free) or `VM.Standard3.Flex` (high RAM) | 4 OCPU / 24 GB is the A1 ceiling |
| Block volume | Persistent vector storage | Attached read/write at `/mnt/vector-lake`, holds `storage/` and `snapshots/` |
| Cloud-init | Docker + Compose, clone, `.env`, `compose up -d --build` | No secret in user-data; `.env` is created from the operator's values |

## Contract with the compose stack

- `VECTOR_STORAGE_DIR` and `VECTOR_SNAPSHOT_DIR` point at the block-volume mount
  path on the host, so the bind mounts in `docker/docker-compose.yml` are
  unchanged between laptop and cloud.
- `QDRANT_API_KEY` arrives through the gitignored `.env`; it is never written
  into Terraform state, user-data, or a tracked file.
- The security list publishes only the retrieval API. Direct database access is
  a private-network or SSH-tunnel operation.

## Planned variables (Stage 2)

```hcl
tenancy_ocid        = # from terraform.tfvars (gitignored)
compartment_ocid    = # from terraform.tfvars (gitignored)
region              = "eu-frankfurt-1"
instance_shape      = "VM.Standard.A1.Flex"
instance_ocpus      = 4
instance_memory_gbs = 24
boot_volume_size_gbs = 100
block_volume_size_gbs = 200
allowed_api_cidr    = "203.0.113.10/32" # documentation range placeholder
ssh_public_key      = "ssh-ed25519 PLACEHOLDER_PUBLIC_KEY operator@example.invalid"
```

State, plans and `.tfvars` stay local. `terraform.tfvars.example` will ship with
placeholders only; the real file is gitignored.
