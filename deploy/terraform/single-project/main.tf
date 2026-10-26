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
  services = [
    "discoveryengine.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com"
  ]
}

module "agent_search" {
  source          = "./modules/agent-search"
  project_id      = var.project_id
  search_location = var.search_location
  search_tier     = var.search_tier
  search_add_ons  = var.search_add_ons

  depends_on = [module.project_services]
}

module "cloudbuild" {
  source                      = "./modules/cloudbuild"
  project_id                  = var.project_id
  region                      = var.region
  corpus_bucket_name          = data.google_storage_bucket.corpus_bucket.name
  github_connection_ready     = var.github_connection_ready
  github_app_installation_id  = var.github_app_installation_id
  github_oauth_secret_version = var.github_oauth_secret_version
  enable_nightly_schedule     = var.enable_nightly_schedule
  enable_ci_triggers          = var.enable_ci_triggers

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
