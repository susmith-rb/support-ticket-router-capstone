mock_provider "google" {}
mock_provider "google-beta" {}

override_resource {
  target = module.agent_search.google_discovery_engine_data_store.faq_articles
}

override_resource {
  target = module.agent_search.google_discovery_engine_data_store.resolved_threads
}

override_resource {
  target = module.agent_search.google_discovery_engine_search_engine.support_kb_search
}

run "verify_agent_search_defaults" {
  command = plan

  assert {
    condition     = module.agent_search.search_location == "global"
    error_message = "Expected search_location to default to global"
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
    condition     = module.agent_search.faq_articles_content_config == "CONTENT_REQUIRED"
    error_message = "Expected faq-articles content_config to be CONTENT_REQUIRED"
  }

  assert {
    condition     = module.agent_search.resolved_threads_data_store_id == "resolved-threads"
    error_message = "Expected resolved-threads data store ID"
  }

  assert {
    condition     = module.agent_search.resolved_threads_content_config == "NO_CONTENT"
    error_message = "Expected resolved-threads content_config to be NO_CONTENT"
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
