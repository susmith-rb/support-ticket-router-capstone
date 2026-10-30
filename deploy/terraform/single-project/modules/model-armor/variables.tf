variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region for Model Armor template"
  type        = string
  default     = "us-central1"
}

variable "template_id" {
  description = "ID of the Model Armor template"
  type        = string
  default     = "support-router-lean"
}

variable "triage_sa_email" {
  description = "Email of the sa-triage service account"
  type        = string
}

variable "faq_sa_email" {
  description = "Email of the sa-faq service account"
  type        = string
}

variable "labels" {
  description = "Resource labels"
  type        = map(string)
  default     = {}
}

variable "triage_agent_principal" {
  description = "SPIFFE IAM principal (principal://...) for TriageRouter Agent Runtime identity (REQ-AUTH-004)"
  type        = string
  default     = ""
}

variable "faq_agent_principal" {
  description = "SPIFFE IAM principal (principal://...) for FaqResolver GKE Agent Identity (REQ-AUTH-008)"
  type        = string
  default     = ""
}
