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

variable "enable_cd_trigger" {
  description = "Whether the CD deployment trigger is enabled (push to main)"
  type        = bool
  default     = true
}

variable "cd_approvers" {
  description = "List of identities granted roles/cloudbuild.builds.approver for CD deployment"
  type        = list(string)
  default     = ["user:susmith@gcp.altostrat.com"]
}

variable "triage_agent_gateway" {
  description = "Agent Gateway (deploy/terraform/agent-gateway output gateway_id) bound to TriageRouter by CD (REQ-AUTH-007)."
  type        = string
  default     = ""
}

variable "triage_resource_name" {
  description = "Resource name of the TriageRouter Agent Engine instance for deployment update"
  type        = string
  default     = "projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
}

variable "laya_region" {
  description = "Region for router-s1 endpoint and serving Artifact Registry repository"
  type        = string
  default     = "us-central1"
}

variable "enable_router_endpoint" {
  description = "Whether to deploy the Vertex AI router endpoint resource (empty endpoint is free)"
  type        = bool
  default     = false
}

variable "cloud_sql_tier" {
  description = "Machine type/tier for Cloud SQL PostgreSQL (db-custom-1-3840)"
  type        = string
  default     = "db-custom-1-3840"
}

variable "cloud_sql_availability_type" {
  description = "Cloud SQL availability: ZONAL (cost-effective capstone default) or REGIONAL (production HA)"
  type        = string
  default     = "ZONAL"
}

variable "cloud_sql_deletion_protection" {
  description = "Whether deletion protection is enabled on Cloud SQL instance"
  type        = bool
  default     = true
}

variable "cloud_sql_human_iam_users" {
  description = "List of human IAM user email addresses granted read-only Cloud SQL access"
  type        = list(string)
  default     = ["susmith@gcp.altostrat.com"]
}

variable "agents_image" {
  description = "Docker image for shared Cloud Run services (ticket-db-mcp, order-lookup-mcp, human-queue). Empty string disables Cloud Run services until image is built."
  type        = string
  default     = ""
}

variable "enable_re_service_agent_bindings" {
  description = "Gate IAM binding for Reasoning Engine service agent (service-<NUM>@gcp-sa-aiplatform-re.iam.gserviceaccount.com) until after first Agent Engine / Reasoning Engine deploy lazily creates the service identity."
  type        = bool
  default     = false
}


variable "gke_cluster_name" {
  description = "Name of the GKE Autopilot cluster for FaqResolver"
  type        = string
  default     = "support-agents"
}

variable "gke_deletion_protection" {
  description = "Whether deletion protection is enabled on GKE Autopilot cluster"
  type        = bool
  default     = true
}

variable "cloud_run_min_instances" {
  description = "Minimum instances for Cloud Run services (0 for minimal cost posture)"
  type        = number
  default     = 0
}

variable "cloud_run_max_instances" {
  description = "Maximum instances for Cloud Run services"
  type        = number
  default     = 3
}

variable "manage_ge_search_engine" {
  description = "Whether Terraform manages the google_discovery_engine_search_engine resource for the Gemini Enterprise Web App"
  type        = bool
  default     = true
}

variable "cct_mode" {
  description = "Customer context token mode for order-lookup-mcp (REQ-AUTH-003): off | accept | require."
  type        = string
  default     = "accept"

  validation {
    condition     = contains(["off", "accept", "require"], var.cct_mode)
    error_message = "cct_mode must be one of off, accept, require."
  }
}

variable "triage_agent_principal" {
  description = "SPIFFE IAM principal (principal://...) for TriageRouter Agent Runtime identity (REQ-AUTH-004). Empty string grants nothing."
  type        = string
  default     = ""
}

variable "faq_agent_principal" {
  description = "SPIFFE IAM principal (principal://...) for FaqResolver Agent identity on GKE (REQ-AUTH-004). Empty string grants nothing."
  type        = string
  default     = ""
}

variable "allowed_caller_spiffe_ids" {
  description = "Comma-separated SPIFFE IDs allowed to call order-lookup-mcp (REQ-AUTH-009). Empty keeps the staged (log-only) behaviour."
  type        = string
  default     = ""
}

variable "human_queue_url" {
  description = "Base HTTPS URL of the human-queue Cloud Run service (breaks dependency cycle between pubsub and cloud_run_services)"
  type        = string
  default     = "https://human-queue-34mwh7k34a-uc.a.run.app"
}
