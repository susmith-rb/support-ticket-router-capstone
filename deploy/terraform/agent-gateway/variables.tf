variable "project_id" {
  description = "GCP Project ID"
  type        = string
  default     = "susmith-fde-ticket-router"
}

variable "region" {
  description = "GCP Region for Agent Gateway and Agent Registry resources"
  type        = string
  default     = "us-central1"
}

variable "enforcement_mode" {
  description = "Enforcement mode for IAP authz extension: DRY_RUN (logs only) or ENFORCE (strict blocking)"
  type        = string
  default     = "DRY_RUN"

  validation {
    condition     = contains(["DRY_RUN", "ENFORCE"], var.enforcement_mode)
    error_message = "enforcement_mode must be either 'DRY_RUN' or 'ENFORCE'."
  }
}

variable "triage_agent_principal" {
  description = "SPIFFE agent identity principal of the TriageRouter Agent Engine instance allowed to egress via Agent Gateway"
  type        = string
  default     = "principal://agents.global.org-349636765856.system.id.goog/resources/aiplatform/projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
}

variable "ticket_db_mcp_host" {
  description = "Hostname of the ticket-db-mcp Cloud Run service"
  type        = string
  default     = "ticket-db-mcp-34mwh7k34a-uc.a.run.app"
}

variable "router_s1_dedicated_host" {
  description = "Dedicated endpoint host for router-s1 Vertex AI prediction endpoint"
  type        = string
  default     = "618895853821624320.us-central1-620485621180.prediction.vertexai.goog"
}
