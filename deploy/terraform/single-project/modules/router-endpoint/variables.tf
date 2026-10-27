# deploy/terraform/single-project/modules/router-endpoint/variables.tf

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "laya_region" {
  description = "Region for Vertex AI Endpoint and Artifact Registry repository"
  type        = string
  default     = "us-central1"
}

variable "corpus_bucket_name" {
  description = "Existing GCS bucket storing model checkpoints and corpus files"
  type        = string
}

variable "enable_router_endpoint" {
  description = "Whether to create the Vertex AI Endpoint (empty endpoint is free)"
  type        = bool
  default     = false
}

variable "labels" {
  description = "Resource labels applied to resources supporting labels"
  type        = map(string)
  default     = {}
}

variable "cloudbuild_sa_email" {
  description = "Email of the Cloud Build service account allowed to deploy models to Vertex AI"
  type        = string
  default     = ""
}
