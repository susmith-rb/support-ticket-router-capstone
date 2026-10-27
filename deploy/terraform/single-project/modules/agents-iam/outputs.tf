# deploy/terraform/single-project/modules/agents-iam/outputs.tf

output "sa_triage_email" {
  description = "Email of the TriageRouter service account"
  value       = google_service_account.sa_triage.email
}

output "sa_triage_name" {
  description = "Fully-qualified name of the TriageRouter service account"
  value       = google_service_account.sa_triage.name
}

output "sa_faq_email" {
  description = "Email of the FaqResolver service account"
  value       = google_service_account.sa_faq.email
}

output "sa_faq_name" {
  description = "Fully-qualified name of the FaqResolver service account"
  value       = google_service_account.sa_faq.name
}
