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
    "container.googleapis.com",
    "pubsub.googleapis.com",
    "compute.googleapis.com",
    "identitytoolkit.googleapis.com",
    "apikeys.googleapis.com",
    "modelarmor.googleapis.com",
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",
    "cloudkms.googleapis.com",
    "dlp.googleapis.com"
  ]
}

module "cloud_sql" {
  source              = "./modules/cloud-sql"
  project_id          = var.project_id
  region              = var.region
  tier                = var.cloud_sql_tier
  availability_type   = var.cloud_sql_availability_type
  deletion_protection = var.cloud_sql_deletion_protection
  human_iam_users     = var.cloud_sql_human_iam_users
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

module "ge_app" {
  source                    = "./modules/ge-app"
  project_id                = var.project_id
  location                  = var.search_location
  discovery_engine_sa_email = google_project_service_identity.discovery_engine.email
  manage_ge_search_engine   = var.manage_ge_search_engine
  labels                    = var.labels

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
  enable_cd_trigger           = var.enable_cd_trigger
  cd_approvers                = var.cd_approvers
  triage_resource_name        = var.triage_resource_name
  triage_agent_gateway        = var.triage_agent_gateway
  faq_agent_identity          = var.faq_agent_identity

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

# -----------------------------------------------------------------------------
# Agent Tier Infrastructure
# -----------------------------------------------------------------------------

module "pubsub" {
  source        = "./modules/pubsub"
  project_id    = var.project_id
  push_endpoint = "${var.human_queue_url}/pubsub/escalations"
  labels        = var.labels

  depends_on = [module.project_services]
}

module "network" {
  source     = "./modules/network"
  project_id = var.project_id
  region     = var.region

  depends_on = [module.project_services]
}

module "gke_autopilot" {
  source                        = "./modules/gke-autopilot"
  project_id                    = var.project_id
  region                        = var.region
  cluster_name                  = var.gke_cluster_name
  network                       = module.network.network_id
  subnetwork                    = module.network.subnetwork_id
  cluster_secondary_range_name  = module.network.pods_secondary_range_name
  services_secondary_range_name = module.network.services_secondary_range_name
  deletion_protection           = var.gke_deletion_protection
  labels                        = var.labels

  depends_on = [module.project_services, module.network]
}

module "agents_iam" {
  source                           = "./modules/agents-iam"
  project_id                       = var.project_id
  region                           = var.region
  router_endpoint_id               = module.router_endpoint.endpoint_id
  dedicated_router_endpoint_id     = module.router_endpoint.dedicated_endpoint_id
  sa_queue_email                   = module.cloud_sql.sa_queue_email
  sa_ticket_mcp_email              = module.cloud_sql.sa_ticket_mcp_email
  sa_order_mcp_email               = module.cloud_sql.sa_order_mcp_email
  pubsub_human_queue_topic_name    = module.pubsub.topic_name
  pubsub_human_queue_sub_name      = module.pubsub.subscription_name
  corpus_bucket_name               = data.google_storage_bucket.corpus_bucket.name
  enable_re_service_agent_bindings = var.enable_re_service_agent_bindings
  triage_agent_principal           = var.triage_agent_principal
  faq_agent_principal              = var.faq_agent_principal

  depends_on = [module.project_services, module.pubsub, module.cloud_sql, module.router_endpoint]
}

module "kms_identity" {
  source                      = "./modules/kms-identity"
  project_id                  = var.project_id
  region                      = var.region
  signer_members              = compact(["serviceAccount:${module.agents_iam.sa_triage_email}", var.triage_agent_principal])
  verifier_members            = ["serviceAccount:${module.cloud_sql.sa_order_mcp_email}"]
  redaction_encrypter_members = compact(["serviceAccount:${module.agents_iam.sa_triage_email}", var.triage_agent_principal])
  redaction_decrypter_members = ["serviceAccount:${module.cloud_sql.sa_queue_email}"]
  labels                      = var.labels

  depends_on = [module.project_services, module.agents_iam, module.cloud_sql]
}

module "cloud_run_services" {
  source                    = "./modules/cloud-run-services"
  project_id                = var.project_id
  region                    = var.region
  agents_image              = var.agents_image
  cloud_sql_connection_name = module.cloud_sql.instance_connection_name
  sa_ticket_mcp_email       = module.cloud_sql.sa_ticket_mcp_email
  sa_order_mcp_email        = module.cloud_sql.sa_order_mcp_email
  sa_queue_email            = module.cloud_sql.sa_queue_email
  sa_triage_email           = module.agents_iam.sa_triage_email
  sa_faq_email              = module.agents_iam.sa_faq_email
  triage_agent_principal    = var.triage_agent_principal
  min_instance_count        = var.cloud_run_min_instances
  max_instance_count        = var.cloud_run_max_instances
  cct_key_prefix            = module.kms_identity.key_prefix
  cct_mode                  = var.cct_mode
  push_audience             = "${var.human_queue_url}/pubsub/escalations"
  sa_queue_push_email       = module.pubsub.sa_queue_push_email
  faq_agent_principal       = var.faq_agent_principal
  allowed_caller_spiffe_ids = var.allowed_caller_spiffe_ids
  redaction_kms_key         = module.kms_identity.redaction_map_wrapper_key_id
  purge_audience            = "${var.human_queue_url}/internal/purge-expired-redaction-maps"
  purge_invoker_email       = "sa-redaction-purge@${var.project_id}.iam.gserviceaccount.com"
  labels                    = var.labels

  depends_on = [module.project_services, module.cloud_sql, module.agents_iam, module.kms_identity]
}

module "redaction_purge" {
  source                   = "./modules/redaction-purge"
  project_id               = var.project_id
  region                   = var.region
  human_queue_url          = var.human_queue_url
  human_queue_service_name = "human-queue"

  depends_on = [module.project_services, module.cloud_run_services]
}

module "identity" {
  source     = "./modules/identity"
  project_id = var.project_id
  labels     = var.labels

  depends_on = [module.project_services]
}

module "model_armor" {
  source          = "./modules/model-armor"
  project_id      = var.project_id
  region          = var.region
  template_id     = "support-router-lean"
  triage_sa_email = module.agents_iam.sa_triage_email
  faq_sa_email    = module.agents_iam.sa_faq_email
  labels          = var.labels

  triage_agent_principal = var.triage_agent_principal
  faq_agent_principal    = var.faq_agent_principal

  depends_on = [module.project_services, module.agents_iam]
}



