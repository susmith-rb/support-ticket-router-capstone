variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region"
  type        = string
  default     = "us-central1"
}

variable "router_endpoint_id" {
  description = "Resource ID of the router-s1 Vertex AI endpoint"
  type        = string
  default     = ""
}

variable "dedicated_router_endpoint_id" {
  description = "Resource ID of the dedicated router-s1 Vertex AI endpoint"
  type        = string
  default     = ""
}


variable "sa_queue_email" {
  description = "Email of the sa-queue service account"
  type        = string
  default     = ""
}

variable "sa_ticket_mcp_email" {
  description = "Email of the sa-ticket-mcp service account"
  type        = string
  default     = ""
}

variable "sa_order_mcp_email" {
  description = "Email of the sa-order-mcp service account"
  type        = string
  default     = ""
}

variable "pubsub_human_queue_topic_name" {
  description = "Name of the human queue Pub/Sub topic"
  type        = string
  default     = ""
}

variable "pubsub_human_queue_sub_name" {
  description = "Name of the human queue Pub/Sub subscription"
  type        = string
  default     = ""
}

variable "corpus_bucket_name" {
  description = "Staging/corpus bucket name for Agent Engine staging artifacts"
  type        = string
  default     = ""
}

variable "enable_re_service_agent_bindings" {
  description = "Gate IAM binding for Reasoning Engine service agent (service-<NUM>@gcp-sa-aiplatform-re.iam.gserviceaccount.com) until after first Agent Engine / Reasoning Engine deploy lazily creates the service identity."
  type        = bool
  default     = false
}

