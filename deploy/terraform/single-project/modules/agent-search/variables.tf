variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "search_location" {
  description = "Discovery Engine location (e.g. us, global, eu)"
  type        = string
  default     = "us"
}

variable "search_tier" {
  description = "Search tier (SEARCH_TIER_STANDARD or SEARCH_TIER_ENTERPRISE)"
  type        = string
  default     = "SEARCH_TIER_ENTERPRISE"
}

variable "search_add_ons" {
  description = "Search add-ons (e.g. SEARCH_ADD_ON_LLM)"
  type        = list(string)
  default     = ["SEARCH_ADD_ON_LLM"]
}
