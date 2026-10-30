data "google_project" "project" {
  project_id = var.project_id
}

# REQ-MA-001: Cloud DLP Inspect Template for Support Router PII detection
resource "google_data_loss_prevention_inspect_template" "support_router_inspect" {
  parent       = "projects/${var.project_id}/locations/${var.region}"
  template_id  = "support-router-inspect"
  display_name = "Support Router PII Inspection Template"
  description  = "Cloud SDP inspect template for PII detection in support ticket prompts"

  inspect_config {
    min_likelihood = "LIKELY"
    include_quote  = false

    info_types {
      name = "EMAIL_ADDRESS"
    }
    info_types {
      name = "PHONE_NUMBER"
    }
    info_types {
      name = "US_SOCIAL_SECURITY_NUMBER"
    }
    info_types {
      name = "CREDIT_CARD_NUMBER"
    }
    info_types {
      name = "STREET_ADDRESS"
    }
    info_types {
      name = "IBAN_CODE"
    }
  }
}

# REQ-MA-001: Grant Model Armor service agent DLP access to inspect template
resource "google_project_iam_member" "model_armor_dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-modelarmor.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "model_armor_dlp_reader" {
  project = var.project_id
  role    = "roles/dlp.reader"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-modelarmor.iam.gserviceaccount.com"
}

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
      advanced_config {
        inspect_template = google_data_loss_prevention_inspect_template.support_router_inspect.id
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

  depends_on = [
    google_data_loss_prevention_inspect_template.support_router_inspect,
    google_project_iam_member.model_armor_dlp_user,
    google_project_iam_member.model_armor_dlp_reader,
  ]
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
