resource "google_identity_platform_config" "default" {
  provider = google-beta
  project  = var.project_id

  sign_in {
    email {
      enabled           = true
      password_required = true
    }
    # Declared explicitly: the API returns phone_number { enabled = false } and the provider shows a permadiff otherwise.
    phone_number {
      enabled = false
    }
  }

  authorized_domains = distinct(concat(
    [
      "localhost",
      "faq.8-232-184-225.sslip.io",
      "${var.project_id}.firebaseapp.com",
      "${var.project_id}.web.app"
    ],
    var.authorized_domains
  ))

  multi_tenant {
    allow_tenants = false
  }
}

resource "google_apikeys_key" "identity_web" {
  name         = "identity-web"
  display_name = "identity-web"
  project      = var.project_id

  restrictions {
    api_targets {
      service = "identitytoolkit.googleapis.com"
    }
    api_targets {
      service = "securetoken.googleapis.com"
    }
  }

  depends_on = [google_identity_platform_config.default]
}

resource "google_secret_manager_secret" "identity_web_api_key" {
  secret_id = "identity-web-api-key"
  project   = var.project_id

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret_version" "identity_web_api_key" {
  secret      = google_secret_manager_secret.identity_web_api_key.id
  secret_data = google_apikeys_key.identity_web.key_string
}
