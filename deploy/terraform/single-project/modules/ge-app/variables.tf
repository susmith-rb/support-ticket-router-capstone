variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "location" {
  description = "Discovery Engine location (e.g. global, us)"
  type        = string
  default     = "global"
}

variable "ge_data_store_id" {
  description = "Data store ID for the Gemini Enterprise default backing store"
  type        = string
  default     = "support-ge-default-ds"
}

variable "ge_engine_id" {
  description = "Engine ID for the Gemini Enterprise Web App search engine"
  type        = string
  default     = "support-assistant-app"
}

variable "ge_display_name" {
  description = "Display name for the Gemini Enterprise Web App search engine"
  type        = string
  default     = "Global Retail Hub Support Assistant"
}

variable "company_name" {
  description = "Company name for the Gemini Enterprise search engine common config"
  type        = string
  default     = "Global Retail Hub"
}

variable "discovery_engine_sa_email" {
  description = "Email of the Discovery Engine service agent for aiplatform.user role grant"
  type        = string
  default     = ""
}

variable "manage_ge_search_engine" {
  description = "Whether Terraform manages the google_discovery_engine_search_engine resource. Initial APP_TYPE_INTRANET engine creation runs via provision_ge_app.py due to hashicorp/google v6.50.0 schema lacking app_type, then imported into Terraform when manage_ge_search_engine = true."
  type        = bool
  default     = false
}
