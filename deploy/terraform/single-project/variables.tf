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
  default     = "SEARCH_TIER_ENTERPRISE"
}

variable "search_add_ons" {
  description = "Search add-ons for support-kb-search"
  type        = list(string)
  default     = ["SEARCH_ADD_ON_LLM"]
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
  default     = false
}
