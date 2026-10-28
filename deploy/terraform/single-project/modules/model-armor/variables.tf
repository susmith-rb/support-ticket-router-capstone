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
