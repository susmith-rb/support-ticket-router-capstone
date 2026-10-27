# deploy/terraform/single-project/modules/network/versions.tf

terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.50.0"
    }
  }
}
