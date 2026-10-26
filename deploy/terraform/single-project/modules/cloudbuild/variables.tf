variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "Region for Cloud Build triggers and Cloud Scheduler"
  type        = string
  default     = "us-central1"
}

variable "corpus_bucket_name" {
  description = "GCS bucket for corpus and evaluation data"
  type        = string
  default     = "susmith-fde-ticket-router-laya"
}

variable "github_connection_ready" {
  description = "Whether the Cloud Build GitHub connection is authorized"
  type        = bool
  default     = false
}

variable "github_connection_name" {
  description = "Name for the 2nd-gen Cloud Build GitHub connection"
  type        = string
  default     = "github-connection"
}

variable "github_repo_name" {
  description = "Name of the connected repository resource"
  type        = string
  default     = "support-ticket-router-capstone"
}

variable "github_repo_uri" {
  description = "Remote URI for the GitHub repository"
  type        = string
  default     = "https://github.com/cloud-ai-fde/susmith-capstone-support-ticket-router.git"
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

variable "nightly_schedule_cron" {
  description = "Cron schedule for nightly evaluation"
  type        = string
  default     = "0 2 * * *"
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
