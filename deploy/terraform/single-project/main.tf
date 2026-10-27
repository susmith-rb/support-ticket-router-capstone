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
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "run.googleapis.com",
    "container.googleapis.com"
  ]
}

module "cloud_sql" {
  source              = "./modules/cloud-sql"
  project_id          = var.project_id
  region              = var.region
  tier                = var.cloud_sql_tier
  availability_type   = var.cloud_sql_availability_type
  deletion_protection = var.cloud_sql_deletion_protection
  labels              = var.labels

  depends_on = [module.project_services]
}

module "agent_search" {
  source          = "./modules/agent-search"
  project_id      = var.project_id
  search_location = var.search_location
  search_tier     = var.search_tier
  search_add_ons  = var.search_add_ons

  depends_on = [module.project_services]
}

module "router_endpoint" {
  source                 = "./modules/router-endpoint"
  project_id             = var.project_id
  laya_region            = var.laya_region
  corpus_bucket_name     = data.google_storage_bucket.corpus_bucket.name
  enable_router_endpoint = var.enable_router_endpoint
  labels                 = var.labels
  cloudbuild_sa_email    = module.cloudbuild.cloudbuild_service_account

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
