variable "project_id" {
  description = "GCP Project ID for Terraform state bootstrap"
  type        = string
  default     = "susmith-fde-ticket-router"
}

variable "region" {
  description = "GCP Region for the Terraform state bucket"
  type        = string
  default     = "us-central1"
}

variable "state_bucket_name" {
  description = "Name of the GCS bucket storing Terraform state"
  type        = string
  default     = "susmith-fde-ticket-router-tfstate"
}

variable "labels" {
  description = "Resource labels applied to the state bucket"
  type        = map(string)
  default = {
    cost_center = "infrastructure"
  }
}
