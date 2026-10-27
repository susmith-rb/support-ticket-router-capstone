variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "topic_name" {
  description = "Name of the human queue Pub/Sub topic"
  type        = string
  default     = "human-queue"
}

variable "labels" {
  description = "Resource labels applied to Pub/Sub topics"
  type        = map(string)
  default     = {}
}
