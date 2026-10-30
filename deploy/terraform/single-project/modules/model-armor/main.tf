resource "google_model_armor_template" "support_router_lean" {
  location    = var.region
  template_id = var.template_id
  project     = var.project_id
  labels      = var.labels

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }

    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }

    rai_settings {
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }

    sdp_settings {
      basic_config {
        filter_enforcement = "ENABLED"
      }
    }
  }

  # API requires template_metadata on update; pin server defaults to avoid a
  # permadiff that nulls the block (400 "The 'template_metadata' field is required").
  template_metadata {
    log_sanitize_operations            = false
    log_template_operations            = false
    ignore_partial_invocation_failures = false
  }
}

resource "google_project_iam_member" "model_armor_user_triage" {
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${var.triage_sa_email}"
}

resource "google_project_iam_member" "model_armor_user_triage_agent" {
  count   = var.triage_agent_principal != "" ? 1 : 0
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = var.triage_agent_principal
}

# TODO(REQ-AUTH-008): remove after live e2e
resource "google_project_iam_member" "model_armor_user_faq" {
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${var.faq_sa_email}"
}

# REQ-AUTH-008: FaqResolver GKE Agent Identity Model Armor user
resource "google_project_iam_member" "model_armor_user_faq_agent" {
  count   = var.faq_agent_principal != "" ? 1 : 0
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = var.faq_agent_principal
}
