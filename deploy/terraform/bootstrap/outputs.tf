output "state_bucket_name" {
  description = "Name of the created Terraform state bucket"
  value       = google_storage_bucket.tf_state.name
}

output "state_bucket_url" {
  description = "URL of the created Terraform state bucket"
  value       = google_storage_bucket.tf_state.url
}
