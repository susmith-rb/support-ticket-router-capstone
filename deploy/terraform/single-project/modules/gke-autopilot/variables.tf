variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region for GKE Autopilot cluster"
  type        = string
  default     = "us-central1"
}

variable "cluster_name" {
  description = "Name of the GKE Autopilot cluster"
  type        = string
  default     = "support-agents"
}

variable "network" {
  description = "VPC network name or self link"
  type        = string
}

variable "subnetwork" {
  description = "VPC subnetwork name or self link"
  type        = string
}

variable "cluster_secondary_range_name" {
  description = "Secondary range name for GKE pods"
  type        = string
  default     = "gke-pods"
}

variable "services_secondary_range_name" {
  description = "Secondary range name for GKE services"
  type        = string
  default     = "gke-services"
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the GKE cluster"
  type        = bool
  default     = true
}

variable "labels" {
  description = "Resource labels applied to the GKE cluster"
  type        = map(string)
  default     = {}
}
