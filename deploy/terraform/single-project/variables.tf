variable "project_id" {
  description = "GCP Project ID"
  type        = string
  default     = "susmith-fde-ticket-router"
}

variable "region" {
  description = "Default regional compute and storage region"
  type        = string
  default     = "us-central1"
}

variable "search_location" {
  description = "Multi-region location for Agent Search (Discovery Engine)"
  type        = string
  default     = "global"
}

variable "search_tier" {
  description = "Search tier for support-kb-search"
  type        = string
  default     = "SEARCH_TIER_STANDARD"
}

variable "search_add_ons" {
  description = "Search add-ons for support-kb-search"
  type        = list(string)
  default     = []
}

variable "labels" {
  description = "Resource labels applied to resources supporting labels"
  type        = map(string)
  default = {
    cost_center = "support-ticket-router"
  }
}

variable "corpus_bucket_name" {
  description = "Existing GCS bucket storing raw/processed corpus files"
  type        = string
  default     = "susmith-fde-ticket-router-laya"
}

variable "github_connection_ready" {
  description = "Whether the Cloud Build GitHub connection is authorized"
  type        = bool
  default     = true
}

variable "github_app_installation_id" {
  description = "GitHub App Installation ID (used when connection is ready)"
  type        = number
  default     = 124561748
}

variable "github_oauth_secret_version" {
  description = "Secret Manager secret version storing the GitHub OAuth/PAT token"
  type        = string
  default     = "projects/susmith-fde-ticket-router/locations/us-central1/secrets/github-connection-github-oauthtoken-2a4cdb/versions/latest"
}

variable "enable_nightly_schedule" {
  description = "Whether the Cloud Scheduler nightly evaluation job is enabled (paused if false)"
  type        = bool
  default     = false
}

variable "enable_ci_triggers" {
  description = "Whether CI/CD triggers are enabled to run on push/PR (disabled if false)"
  type        = bool
  default     = true
}

variable "laya_region" {
  description = "Region for router-s1 endpoint and serving Artifact Registry repository"
  type        = string
  default     = "us-central1"
}

variable "enable_router_endpoint" {
  description = "Whether to deploy the Vertex AI router endpoint resource (empty endpoint is free)"
  type        = bool
  default     = true
}
