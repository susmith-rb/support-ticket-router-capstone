variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region for Cloud Run services"
  type        = string
  default     = "us-central1"
}

variable "agents_image" {
  description = "Docker image for shared Cloud Run services (if empty, services have count=0)"
  type        = string
  default     = ""
}

variable "cloud_sql_connection_name" {
  description = "Cloud SQL instance connection name (<project>:<region>:<instance>)"
  type        = string
}

variable "sa_ticket_mcp_email" {
  description = "Email of the sa-ticket-mcp service account"
  type        = string
}

variable "sa_order_mcp_email" {
  description = "Email of the sa-order-mcp service account"
  type        = string
}

variable "sa_queue_email" {
  description = "Email of the sa-queue service account"
  type        = string
}

variable "sa_triage_email" {
  description = "Email of the sa-triage service account (invoker)"
  type        = string
  default     = ""
}

variable "sa_faq_email" {
  description = "Email of the sa-faq service account (invoker)"
  type        = string
  default     = ""
}

variable "min_instance_count" {
  description = "Minimum instances for Cloud Run services (0 for minimal capstone posture)"
  type        = number
  default     = 0
}

variable "max_instance_count" {
  description = "Maximum instances for Cloud Run services"
  type        = number
  default     = 3
}

variable "labels" {
  description = "Resource labels applied to Cloud Run services"
  type        = map(string)
  default     = {}
}

variable "cct_mode" {
  description = "Customer context token verification mode for order-lookup-mcp (off, accept, require)"
  type        = string
  default     = "accept"
}

variable "cct_key_prefix" {
  description = "Allowed KMS key resource prefix for CCT verification"
  type        = string
  default     = ""
}

variable "triage_agent_principal" {
  description = "SPIFFE IAM principal (principal://...) for TriageRouter Agent Runtime identity (REQ-AUTH-004)"
  type        = string
  default     = ""
}

variable "push_audience" {
  description = "Expected audience for Pub/Sub push subscription OIDC tokens"
  type        = string
  default     = ""
}

variable "sa_queue_push_email" {
  description = "Email of the sa-queue-push service account authorized to invoke human-queue push endpoint"
  type        = string
  default     = ""
}

variable "faq_agent_principal" {
  description = "SPIFFE IAM principal (principal://...) for FaqResolver GKE Agent Identity (REQ-AUTH-008)"
  type        = string
  default     = ""
}

variable "allowed_caller_spiffe_ids" {
  description = "Comma-separated list of allowed caller SPIFFE IDs for order-lookup-mcp (REQ-AUTH-009)"
  type        = string
  default     = ""
}

variable "redaction_kms_key" {
  description = "KMS key resource name for redaction envelope encryption"
  type        = string
  default     = ""
}

variable "purge_audience" {
  description = "Expected audience for purge endpoint invocations"
  type        = string
  default     = ""
}

variable "purge_invoker_email" {
  description = "Email of service account authorized to invoke purge endpoint"
  type        = string
  default     = ""
}

variable "review_ui_reps" {
  description = "List of IAM principals granted roles/iap.httpsResourceAccessor on human-review-ui"
  type        = list(string)
  default     = ["user:susmith@gcp.altostrat.com"]
}

variable "human_queue_url" {
  description = "Fallback URL of human-queue service (used when service_count == 0)"
  type        = string
  default     = ""
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on Cloud Run services"
  type        = bool
  default     = false
}

variable "triage_fallback_min_instances" {
  description = "Minimum instances for warm triage-fallback Cloud Run service (REQ-AGENT-035)"
  type        = number
  default     = 1
}

variable "triage_fallback_invokers" {
  description = "List of IAM member strings granted roles/run.invoker on triage-fallback"
  type        = list(string)
  default     = []
}

variable "discovery_engine_sa_email" {
  description = "Email of Discovery Engine service agent"
  type        = string
  default     = ""
}

variable "router_s1_endpoint" {
  description = "Resource ID of the dedicated router-s1 Vertex AI Endpoint"
  type        = string
  default     = "projects/susmith-fde-ticket-router/locations/us-central1/endpoints/router-s1-dedicated"
}

variable "router_s1_base_url" {
  description = "Base URL of the dedicated router-s1 Vertex AI Endpoint"
  type        = string
  default     = "https://router-s1-dedicated.us-central1-620485621180.prediction.vertexai.goog"
}

variable "faq_resolver_url" {
  description = "Public HTTPS URL for FaqResolver on GKE Autopilot via sslip.io"
  type        = string
  default     = "https://faq.8-232-184-225.sslip.io"
}

variable "faq_resolver_audience" {
  description = "Target audience URI for Google ID token authentication on FaqResolver"
  type        = string
  default     = "https://faq.8-232-184-225.sslip.io"
}

variable "allowed_callers" {
  description = "Allowed caller service accounts/principals for TriageRouter (defaults to sa_triage_email)"
  type        = string
  default     = ""
}

variable "model_armor_template" {
  description = "Full resource name of the Model Armor template"
  type        = string
  default     = "projects/susmith-fde-ticket-router/locations/us-central1/templates/support-router-lean"
}

variable "model_armor_enabled" {
  description = "Whether Model Armor sanitization is enabled (1 or 0)"
  type        = string
  default     = "1"
}

variable "human_queue_topic" {
  description = "Pub/Sub topic for human queue escalations"
  type        = string
  default     = "human-queue"
}

variable "agent_search_location" {
  description = "Location for Agent Search datastores"
  type        = string
  default     = "global"
}

variable "agent_search_engine_id" {
  description = "Engine ID for support-kb-search"
  type        = string
  default     = "support-kb-search"
}

variable "cct_key_version" {
  description = "KMS key version for customer context signing"
  type        = string
  default     = ""
}

variable "cct_issuer" {
  description = "Issuer claim for customer context tokens"
  type        = string
  default     = "triage-router"
}

variable "cct_audience" {
  description = "Audience claim for customer context tokens"
  type        = string
  default     = "order-lookup-mcp"
}

variable "cct_ttl_s" {
  description = "TTL in seconds for customer context tokens"
  type        = string
  default     = "300"
}

variable "require_real_deps" {
  description = "Whether to enforce real dependencies on TriageRouter (1 or 0)"
  type        = string
  default     = "1"
}

variable "ge_oauth_auth_id" {
  description = "OAuth authorization ID for Gemini Enterprise front-door"
  type        = string
  default     = ""
}



