# deploy/terraform/single-project/modules/kms-identity/outputs.tf

output "key_ring_id" {
  description = "Resource ID of the identity KMS key ring"
  value       = google_kms_key_ring.ticket_router_identity.id
}

output "crypto_key_id" {
  description = "Resource ID of the customer context signing crypto key"
  value       = google_kms_crypto_key.customer_context_signer.id
}

output "signing_key_version" {
  description = "Resource name of the primary asymmetric signing key version"
  value       = "${google_kms_crypto_key.customer_context_signer.id}/cryptoKeyVersions/1"
}

output "key_prefix" {
  description = "Key resource name prefix for verification key validation"
  value       = "${google_kms_crypto_key.customer_context_signer.id}/"
}
