mock_provider "google" {}
mock_provider "google-beta" {}

run "verify_agent_search_defaults" {
  command = plan

  assert {
    condition     = module.agent_search.search_location == "us"
    error_message = "Expected search_location to default to us"
  }

  assert {
    condition     = module.agent_search.search_tier == "SEARCH_TIER_ENTERPRISE"
    error_message = "Expected search_tier to default to SEARCH_TIER_ENTERPRISE"
  }

  assert {
    condition     = module.agent_search.faq_articles_data_store_id == "faq-articles"
    error_message = "Expected faq-articles data store ID"
  }

  assert {
    condition     = module.agent_search.resolved_threads_data_store_id == "resolved-threads"
    error_message = "Expected resolved-threads data store ID"
  }

  assert {
    condition     = module.agent_search.search_engine_id == "support-kb-search"
    error_message = "Expected support-kb-search engine ID"
  }

  assert {
    condition     = length(var.labels) > 0 && var.labels["cost_center"] == "support-ticket-router"
    error_message = "Expected cost_center label to be set"
  }
}

run "verify_custom_tier_and_location" {
  command = plan

  variables {
    search_location = "us"
    search_tier     = "SEARCH_TIER_STANDARD"
    search_add_ons  = []
  }

  assert {
    condition     = module.agent_search.search_tier == "SEARCH_TIER_STANDARD"
    error_message = "Expected search_tier to flow through as SEARCH_TIER_STANDARD"
  }

  assert {
    condition     = module.agent_search.search_location == "us"
    error_message = "Expected search_location to remain us"
  }
}
