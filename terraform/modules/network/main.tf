# =============================================================================
# OCI network module — public VCN for the vector lake
# =============================================================================
# PORTABILITY
#
#   | OCI (here)                | AWS                              | GCP                          |
#   | ------------------------- | -------------------------------- | ---------------------------- |
#   | oci_core_vcn              | aws_vpc                          | google_compute_network       |
#   | oci_core_internet_gateway | aws_internet_gateway             | (implicit default route)     |
#   | oci_core_route_table      | aws_route_table + aws_route      | google_compute_route         |
#   | oci_core_security_list    | aws_security_group + aws_vpc_    | google_compute_firewall      |
#   |                           | security_group_rule              |                              |
#   | oci_core_subnet           | aws_subnet                       | google_compute_subnetwork    |
#
# The ingress contract is provider neutral: SSH from ssh_allowed_cidrs, vector
# API ports from vector_api_cidrs. On AWS each pair becomes an ingress rule on
# the instance security group; on GCP each becomes an ingress rule with
# source_ranges. Only this file names OCI resources.
#
# SECURITY POSTURE
#   Inbound  22/tcp       from ssh_allowed_cidrs only (operator /32 or VPN)
#   Inbound  vector_db_ports from vector_api_cidrs only (never 0.0.0.0/0)
#   Inbound  nothing else: no 80/443, no ICMP, no management plane
#   Outbound explicit: 80/443 for packages and images, 53 for DNS
#
# The database is not an internet service. A world-open vector port would be a
# finding, and both variables reject 0.0.0.0/0 at plan time.

locals {
  ssh_ingress = [
    for cidr in var.ssh_allowed_cidrs : {
      port        = 22
      source      = cidr
      description = "SSH from the operator/management CIDR"
    }
  ]

  # Cartesian product: every allowed source gets every configured API port.
  vector_ingress = flatten([
    for cidr in var.vector_api_cidrs : [
      for port in var.vector_db_ports : {
        port        = port
        source      = cidr
        description = "Vector database API from an approved application range"
      }
    ]
  ])

  ingress_rules = concat(local.ssh_ingress, local.vector_ingress)

  ingress_ports   = sort(distinct([for rule in local.ingress_rules : rule.port]))
  ingress_sources = [for rule in local.ingress_rules : { port = rule.port, source = rule.source }]
}

resource "oci_core_vcn" "this" {
  compartment_id = var.compartment_id
  cidr_block     = var.vcn_cidr
  display_name   = "${var.name_prefix}-vcn"
  dns_label      = var.vcn_dns_label
  freeform_tags  = var.freeform_tags
}

resource "oci_core_internet_gateway" "this" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-igw"
  enabled        = true
  freeform_tags  = var.freeform_tags
}

# A dedicated route table keeps routing intent visible in the plan instead of
# mutating the VCN default table.
resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-public-rt"
  freeform_tags  = var.freeform_tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.this.id
    description       = "Default IPv4 egress through the internet gateway"
  }
}

resource "oci_core_security_list" "public" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "${var.name_prefix}-sl"
  freeform_tags  = var.freeform_tags

  # One block per (port, source) pair; the SSH rule is always present.
  dynamic "ingress_security_rules" {
    for_each = local.ingress_rules

    content {
      protocol    = "6" # TCP
      source      = ingress_security_rules.value.source
      source_type = "CIDR_BLOCK"
      description = ingress_security_rules.value.description

      tcp_options {
        min = ingress_security_rules.value.port
        max = ingress_security_rules.value.port
      }
    }
  }

  egress_security_rules {
    protocol         = "6"
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    description      = "OS packages (HTTP)"

    tcp_options {
      min = 80
      max = 80
    }
  }

  egress_security_rules {
    protocol         = "6"
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    description      = "OS packages, container images and model downloads (HTTPS)"

    tcp_options {
      min = 443
      max = 443
    }
  }

  egress_security_rules {
    protocol         = "17" # UDP
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    description      = "DNS resolution"

    udp_options {
      min = 53
      max = 53
    }
  }
}

resource "oci_core_subnet" "public" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.this.id
  cidr_block                 = var.subnet_cidr
  display_name               = "${var.name_prefix}-public-subnet"
  dns_label                  = var.subnet_dns_label
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_security_list.public.id]
  prohibit_public_ip_on_vnic = false
  freeform_tags              = var.freeform_tags
}
