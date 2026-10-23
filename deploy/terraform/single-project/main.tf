provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

module "project_services" {
  source     = "./modules/project-services"
  project_id = var.project_id
}

module "agent_search" {
  source          = "./modules/agent-search"
  project_id      = var.project_id
  search_location = var.search_location
  search_tier     = var.search_tier
  search_add_ons  = var.search_add_ons

  depends_on = [module.project_services]
}

data "google_storage_bucket" "corpus_bucket" {
  name = var.corpus_bucket_name
}

resource "google_project_service_identity" "discovery_engine" {
  provider = google-beta
  project  = var.project_id
  service  = "discoveryengine.googleapis.com"

  depends_on = [module.project_services]
}

resource "google_storage_bucket_iam_member" "discovery_engine_corpus_reader" {
  bucket = data.google_storage_bucket.corpus_bucket.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_project_service_identity.discovery_engine.email}"
}
