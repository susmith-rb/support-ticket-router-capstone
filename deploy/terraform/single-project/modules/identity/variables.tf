variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "labels" {
  description = "Resource labels"
  type        = map(string)
  default     = {}
}

variable "authorized_domains" {
  description = "Additional authorized domains for Identity Platform"
  type        = list(string)
  default     = []
}
