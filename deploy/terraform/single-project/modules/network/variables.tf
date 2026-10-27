# deploy/terraform/single-project/modules/network/variables.tf

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region for subnetwork"
  type        = string
  default     = "us-central1"
}

variable "network_name" {
  description = "Name of the VPC network"
  type        = string
  default     = "support-vpc"
}

variable "subnetwork_name" {
  description = "Name of the subnetwork"
  type        = string
  default     = "support-us-central1"
}

variable "subnet_cidr" {
  description = "Primary CIDR range for support subnet"
  type        = string
  default     = "10.10.0.0/20"
}

variable "pods_cidr" {
  description = "Secondary CIDR range for GKE Autopilot pods"
  type        = string
  default     = "10.20.0.0/14"
}

variable "services_cidr" {
  description = "Secondary CIDR range for GKE Autopilot services"
  type        = string
  default     = "10.24.0.0/20"
}

