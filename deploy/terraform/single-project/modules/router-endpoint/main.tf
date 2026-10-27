# deploy/terraform/single-project/modules/router-endpoint/main.tf
# Provisions Vertex AI Endpoint, Artifact Registry Docker repository, and Service Account for Laya serving.

data "google_project" "project" {
  project_id = var.project_id
}

locals {
  cloudbuild_sa = var.cloudbuild_sa_email != "" ? var.cloudbuild_sa_email : "ticket-router-build@${var.project_id}.iam.gserviceaccount.com"
}

resource "google_artifact_registry_repository" "serving" {
  location      = var.laya_region
  repository_id = "serving"
  description   = "Docker repository for custom Laya router and reranker serving containers"
  format        = "DOCKER"

  labels = var.labels
}

resource "google_service_account" "ticket_router_serving" {
  account_id   = "ticket-router-serving"
  display_name = "Ticket Router Model Serving Service Account"
  description  = "Identity for Vertex AI custom container serving router-s1 and Laya choice reranker"
}

resource "google_storage_bucket_iam_member" "serving_corpus_reader" {
  bucket = var.corpus_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.ticket_router_serving.email}"
}

resource "google_project_iam_member" "serving_logging_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.ticket_router_serving.email}"
}

resource "google_project_iam_member" "serving_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.ticket_router_serving.email}"
}

# Vertex AI Service Agent impersonation on ticket-router-serving
resource "google_service_account_iam_member" "vertex_service_agent_user" {
  service_account_id = google_service_account.ticket_router_serving.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-aiplatform.iam.gserviceaccount.com"
}

# Cloud Build SA impersonation on ticket-router-serving
resource "google_service_account_iam_member" "cloudbuild_serving_sa_user" {
  service_account_id = google_service_account.ticket_router_serving.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.cloudbuild_sa}"
}

# Cloud Build SA permissions for Vertex AI deployment
resource "google_project_iam_member" "cloudbuild_aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${local.cloudbuild_sa}"
}

# Cloud Build SA permissions to push container images to Artifact Registry serving repository
resource "google_artifact_registry_repository_iam_member" "cloudbuild_serving_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.serving.location
  repository = google_artifact_registry_repository.serving.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${local.cloudbuild_sa}"
}

resource "google_vertex_ai_endpoint" "router_s1" {
  count        = var.enable_router_endpoint ? 1 : 0
  name         = "router-s1"
  display_name = "router-s1"
  description  = "Vertex AI Endpoint for System-1 router (ModernBERT) and choice reranker"
  location     = var.laya_region

  labels = var.labels
}
