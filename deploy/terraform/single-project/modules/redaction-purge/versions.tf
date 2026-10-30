# deploy/terraform/single-project/modules/redaction-purge/versions.tf
# Provider and Terraform version constraints for redaction-purge module.

terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0.0"
    }
  }
}
