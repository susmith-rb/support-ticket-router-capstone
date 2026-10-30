terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.20"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 7.20"
    }
  }
  backend "gcs" {
    bucket = "susmith-fde-ticket-router-tfstate"
    prefix = "terraform/state/agent-gateway"
  }
}
