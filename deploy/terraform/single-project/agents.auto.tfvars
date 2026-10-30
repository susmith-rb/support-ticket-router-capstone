# Container image for the agent tier (Cloud Run MCP servers + HumanQueue; GKE FaqResolver uses the same image).
# Bump this after each services/cloudbuild.yaml build; committed so plans stay stable.
agents_image                     = "us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:308dfaa"
enable_re_service_agent_bindings = true
# REQ-AUTH-003: order-lookup requires a verified customer context token (flipped from accept after live e2e 5/5).
cct_mode = "require"
