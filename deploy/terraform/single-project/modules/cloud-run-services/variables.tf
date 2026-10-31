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


