# deploy/terraform/single-project/modules/redaction-purge/variables.tf
# Variable declarations for redaction map TTL purge scheduler.

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Scheduler and Cloud Run resources"
  type        = string
  default     = "us-central1"
}

variable "human_queue_url" {
  description = "Base HTTPS URL of the human-queue Cloud Run service"
  type        = string
}

variable "human_queue_service_name" {
  description = "Name of the human-queue Cloud Run v2 service for invoker IAM binding"
  type        = string
  default     = "human-queue"
}

variable "purge_audience" {
  description = "Optional audience override for Cloud Scheduler OIDC token (defaults to endpoint URL)"
  type        = string
  default     = ""
}

variable "labels" {
  description = "Resource labels applied to created resources"
  type        = map(string)
  default     = {}
}
