terraform {
  required_version = ">= 1.6.0"

  required_providers {
    # Explicit source: without it OpenTofu infers hashicorp/oci, which cannot
    # receive the root module's oracle/oci configuration.
    oci = {
      source  = "oracle/oci"
      version = ">= 6.20.0, < 8.0.0"
    }
  }
}
