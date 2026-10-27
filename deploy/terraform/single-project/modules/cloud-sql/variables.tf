variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region for Cloud SQL instance"
  type        = string
  default     = "us-central1"
}

variable "instance_name" {
  description = "Cloud SQL instance identifier"
  type        = string
  default     = "ticket-store"
}

variable "database_name" {
  description = "Initial PostgreSQL database name"
  type        = string
  default     = "support"
}

variable "tier" {
  description = "Machine type/tier for Cloud SQL PostgreSQL (e.g. db-custom-1-3840)"
  type        = string
  default     = "db-custom-1-3840"
}

variable "availability_type" {
  description = "Availability type: ZONAL (cost-effective capstone default) or REGIONAL (production HA)"
  type        = string
  default     = "ZONAL"
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the Cloud SQL instance"
  type        = bool
  default     = true
}

variable "backup_enabled" {
  description = "Whether automated daily backups and PITR are enabled"
  type        = bool
  default     = true
}

variable "labels" {
  description = "Resource labels applied to resources supporting labels"
  type        = map(string)
  default     = {}
}
