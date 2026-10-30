# Container image for the agent tier (Cloud Run MCP servers + HumanQueue; GKE FaqResolver uses the same image).
# Bump this after each services/cloudbuild.yaml build; committed so plans stay stable.
agents_image                     = "us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:308dfaa"
enable_re_service_agent_bindings = true
# REQ-AUTH-003: order-lookup requires a verified customer context token (flipped from accept after live e2e 5/5).
cct_mode = "require"
# REQ-AUTH-004: SPIFFE agent identity of the PRODUCTION TriageRouter engine (2170218524805431296, promoted from the staging pilot 2026-10-30).
triage_agent_principal = "principal://agents.global.org-349636765856.system.id.goog/resources/aiplatform/projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
