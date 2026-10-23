resource "google_discovery_engine_data_store" "faq_articles" {
  project                     = var.project_id
  location                    = var.search_location
  data_store_id               = "faq-articles"
  display_name                = "FAQ Articles"
  industry_vertical           = "GENERIC"
  content_config              = "CONTENT_REQUIRED"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false

  lifecycle {
    ignore_changes = [
      document_processing_config,
    ]
  }
}

resource "google_discovery_engine_data_store" "resolved_threads" {
  project                     = var.project_id
  location                    = var.search_location
  data_store_id               = "resolved-threads"
  display_name                = "Resolved Threads"
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

resource "google_discovery_engine_search_engine" "support_kb_search" {
  project           = var.project_id
  engine_id         = "support-kb-search"
  collection_id     = "default_collection"
  location          = var.search_location
  display_name      = "Support KB Search"
  industry_vertical = "GENERIC"
  data_store_ids = [
    google_discovery_engine_data_store.faq_articles.data_store_id,
    google_discovery_engine_data_store.resolved_threads.data_store_id,
  ]

  search_engine_config {
    search_tier    = var.search_tier
    search_add_ons = var.search_add_ons
  }
}
