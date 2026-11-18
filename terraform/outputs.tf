# =============================================================================
# Outputs — what an operator or the next stage needs
# =============================================================================
# Public information only: addresses, OCIDs, sizes and an exposure inventory.
# Never a credential, a private key or a command containing a secret.
#
# PORTABILITY: names stay neutral — `instance_public_ip` is an EC2 public IP or
# a GCE external IP; `block_volume_*` is an EBS volume or a Persistent Disk.

output "instance_public_ip" {
  description = "Public IPv4 address of the vector lake host."
  value       = module.compute.public_ip
}

output "instance_private_ip" {
  description = "Private IPv4 address inside the vector lake VCN."
  value       = module.compute.private_ip
}

output "instance_id" {
  description = "OCID of the compute instance."
  value       = module.compute.instance_id
}

output "instance_shape" {
  description = "Shape used, with flexible sizing when applicable."
  value       = "${var.instance_shape} (${var.instance_ocpus} OCPU / ${var.instance_memory_gbs} GB)"
}

output "availability_domain" {
  description = "Availability domain the instance landed in; pin it when A1 capacity is tight."
  value       = module.compute.availability_domain
}

output "image_id" {
  description = "Image OCID used at apply time."
  value       = module.compute.image_id
}

# -----------------------------------------------------------------------------
# Persistent block storage
# -----------------------------------------------------------------------------

output "block_volume_id" {
  description = "OCID of the persistent vector-data block volume."
  value       = module.storage.volume_id
}

output "block_volume_attachment_id" {
  description = "OCID of the volume attachment."
  value       = module.storage.attachment_id
}

output "block_volume_size_gbs" {
  description = "Size of the vector-data volume in GB."
  value       = module.storage.size_in_gbs
}

output "block_volume_vpus_per_gb" {
  description = "Performance units per GB of the data volume."
  value       = module.storage.vpus_per_gb
}

output "block_volume_device" {
  description = "Guest device path OCI reports for the attachment (paravirtualized: /dev/oracleoci/oraclevdb)."
  value       = module.storage.device
}

output "block_volume_mount_point" {
  description = "Host path where cloud-init mounts the data volume."
  value       = module.storage.mount_point
}

# -----------------------------------------------------------------------------
# Network and exposure
# -----------------------------------------------------------------------------

output "vcn_id" {
  description = "OCID of the vector lake VCN."
  value       = module.network.vcn_id
}

output "subnet_id" {
  description = "OCID of the public subnet."
  value       = module.network.subnet_id
}

output "security_list_id" {
  description = "OCID of the security list enforcing the perimeter."
  value       = module.network.security_list_id
}

output "ingress_ports" {
  description = "Inbound TCP ports actually opened (22 plus the configured vector DB ports)."
  value       = module.network.ingress_ports
}

output "ingress_exposure" {
  description = "Port/source pairs for an explicit exposure review. A world-open source here is a finding, not a setting."
  value       = module.network.ingress_sources
}

output "vector_endpoints" {
  description = "Endpoints the application layer uses once the stack is deployed on the host."
  value = {
    qdrant_http = "http://${module.compute.public_ip}:6333"
    qdrant_grpc = "${module.compute.public_ip}:6334"
    # Milvus-compatible alternative: add 19530 to vector_db_ports and use
    # "${module.compute.public_ip}:19530" instead.
  }
}

output "ssh_connection" {
  description = "Copy-paste SSH command. The private key stays on the operator workstation."
  value       = "ssh ubuntu@${module.compute.public_ip}"
}

output "deploy_next_step" {
  description = "What to do after apply. Carries no credential: .env is created on the host."
  value       = <<-EOT
    1. ssh ubuntu@${module.compute.public_ip}
    2. df -h ${module.storage.mount_point}          # confirm the data volume is mounted
    3. git clone https://github.com/kaveh8866/-Distributed-Vector-Embedding-Lake.git
    4. cd -- -Distributed-Vector-Embedding-Lake 2>/dev/null || cd Distributed-Vector-Embedding-Lake
    5. cp .env.example .env                          # set VECTOR_STORAGE_DIR to the mount path
    6. docker compose --env-file .env -f docker/docker-compose.yml up -d --build
  EOT
}
