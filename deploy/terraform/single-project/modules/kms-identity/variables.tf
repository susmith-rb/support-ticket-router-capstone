variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP region for KMS key ring"
  type        = string
  default     = "us-central1"
}

variable "signer_members" {
  description = "List of IAM members granted roles/cloudkms.signer on the customer context signing key"
  type        = list(string)
  default     = []
}

variable "verifier_members" {
  description = "List of IAM members granted roles/cloudkms.publicKeyViewer on the customer context signing key"
  type        = list(string)
  default     = []
}

variable "labels" {
  description = "Resource labels applied to KMS resources"
  type        = map(string)
  default     = {}
}
