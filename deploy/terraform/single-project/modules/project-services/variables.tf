variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "services" {
  description = "List of GCP service APIs to enable"
  type        = list(string)
  default = [
    "discoveryengine.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "sqladmin.googleapis.com",
    "run.googleapis.com",
    "container.googleapis.com",
    "secretmanager.googleapis.com"
  ]
}
