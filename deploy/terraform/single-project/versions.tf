terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.50.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.50.0"
    }
  }
  backend "gcs" {
    bucket = "susmith-fde-ticket-router-tfstate"
    prefix = "terraform/state/single-project"
  }
}
