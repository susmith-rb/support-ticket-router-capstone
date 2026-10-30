# deploy/terraform/single-project/modules/kms-identity/main.tf
# Cloud KMS Key Ring and Asymmetric Signing Key for Customer Context Token (CCT).
# Implements REQ-AUTH-002 and REQ-AUTH-003.

resource "google_kms_key_ring" "ticket_router_identity" {
  name     = "ticket-router-identity"
  location = var.region
  project  = var.project_id

  lifecycle {
    prevent_destroy = true
  }
}

# Key rings and crypto keys cannot be deleted in Cloud KMS (spec Open Question 1).
resource "google_kms_crypto_key" "customer_context_signer" {
  name     = "customer-context-signer"
  key_ring = google_kms_key_ring.ticket_router_identity.id
  purpose  = "ASYMMETRIC_SIGN"

  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = "SOFTWARE"
  }

  lifecycle {
    prevent_destroy = true
  }

  labels = var.labels
}

# IAM member grants on customer-context-signer crypto key
resource "google_kms_crypto_key_iam_member" "signers" {
  for_each      = toset(var.signer_members)
  crypto_key_id = google_kms_crypto_key.customer_context_signer.id
  role          = "roles/cloudkms.signer"
  member        = each.value
}

resource "google_kms_crypto_key_iam_member" "verifiers" {
  for_each      = toset(var.verifier_members)
  crypto_key_id = google_kms_crypto_key.customer_context_signer.id
  role          = "roles/cloudkms.publicKeyViewer"
  member        = each.value
}

# Symmetric key for KMS envelope encryption of PII redaction maps (REQ-STORE-009).
resource "google_kms_crypto_key" "redaction_map_wrapper" {
  name            = "redaction-map-wrapper"
  key_ring        = google_kms_key_ring.ticket_router_identity.id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s"

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPTION"
    protection_level = "SOFTWARE"
  }

  lifecycle {
    prevent_destroy = true
  }

  labels = var.labels
}

# IAM member grants on redaction-map-wrapper crypto key (REQ-STORE-009)
resource "google_kms_crypto_key_iam_member" "redaction_encrypters" {
  for_each      = toset(var.redaction_encrypter_members)
  crypto_key_id = google_kms_crypto_key.redaction_map_wrapper.id
  role          = "roles/cloudkms.cryptoKeyEncrypter"
  member        = each.value
}

resource "google_kms_crypto_key_iam_member" "redaction_decrypters" {
  for_each      = toset(var.redaction_decrypter_members)
  crypto_key_id = google_kms_crypto_key.redaction_map_wrapper.id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  member        = each.value
}
