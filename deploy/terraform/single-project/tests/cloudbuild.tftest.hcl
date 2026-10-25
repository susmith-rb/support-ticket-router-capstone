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

run "verify_cloudbuild_defaults_without_github_connection" {
  command = plan

  assert {
    condition     = module.cloudbuild.scheduler_job_name == "support-ticket-router-nightly-eval"
    error_message = "Expected Cloud Scheduler job name support-ticket-router-nightly-eval"
  }

  assert {
    condition     = module.cloudbuild.github_connection_id == null
    error_message = "Expected github_connection_id to be null when github_connection_ready is false"
  }

  assert {
    condition     = module.cloudbuild.github_repository_id == null
    error_message = "Expected github_repository_id to be null when github_connection_ready is false"
  }

  assert {
    condition     = length(var.labels) > 0 && var.labels["cost_center"] == "support-ticket-router"
    error_message = "Expected cost_center label to be set"
  }
}

run "verify_cloudbuild_with_github_connection_ready" {
  command = plan

  variables {
    github_connection_ready = true
  }

  assert {
    condition     = module.cloudbuild.github_connection_id != null
    error_message = "Expected github_connection_id to be populated when connection is ready"
  }

  assert {
    condition     = module.cloudbuild.github_repository_id != null
    error_message = "Expected github_repository_id to be populated when connection is ready"
  }
}
