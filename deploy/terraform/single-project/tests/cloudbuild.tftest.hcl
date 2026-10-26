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

override_resource {
  target = module.cloudbuild.google_cloudbuildv2_connection.github[0]
}

run "verify_cloudbuild_defaults_with_github_connection" {
  command = apply

  assert {
    condition     = module.cloudbuild.scheduler_job_name == "support-ticket-router-nightly-eval"
    error_message = "Expected Cloud Scheduler job name support-ticket-router-nightly-eval"
  }

  assert {
    condition     = module.cloudbuild.github_connection_id != null
    error_message = "Expected github_connection_id to be populated when connection is ready"
  }

  assert {
    condition     = module.cloudbuild.github_repository_id != null
    error_message = "Expected github_repository_id to be populated when connection is ready"
  }

  assert {
    condition     = length(var.labels) > 0 && var.labels["cost_center"] == "support-ticket-router"
    error_message = "Expected cost_center label to be set"
  }
}

run "verify_cloudbuild_with_nightly_schedule_enabled" {
  command = apply

  variables {
    enable_nightly_schedule = true
  }

  assert {
    condition     = module.cloudbuild.scheduler_job_name == "support-ticket-router-nightly-eval"
    error_message = "Expected Cloud Scheduler job name support-ticket-router-nightly-eval"
  }
}
