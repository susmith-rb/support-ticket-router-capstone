resource "google_discovery_engine_data_store" "ge_default" {
  project                     = var.project_id
  location                    = var.location
  data_store_id               = var.ge_data_store_id
  display_name                = "Support Assistant Backing Store (No Content)"
  industry_vertical           = "GENERIC"
  content_config              = "NO_CONTENT"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false

  lifecycle {
    ignore_changes = [
      document_processing_config,
    ]
  }
}

resource "google_project_iam_member" "discovery_engine_aiplatform_user" {
  count   = var.discovery_engine_sa_email != "" ? 1 : 0
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${var.discovery_engine_sa_email}"
}

resource "google_discovery_engine_search_engine" "support_assistant_app" {
  count             = var.manage_ge_search_engine ? 1 : 0
  project           = var.project_id
  location          = var.location
  collection_id     = "default_collection"
  engine_id         = var.ge_engine_id
  display_name      = var.ge_display_name
  industry_vertical = "GENERIC"
  data_store_ids = [
    google_discovery_engine_data_store.ge_default.data_store_id,
  ]

  common_config {
    company_name = var.company_name
  }

  search_engine_config {
    search_tier    = "SEARCH_TIER_ENTERPRISE"
    search_add_ons = ["SEARCH_ADD_ON_LLM"]
  }
}
