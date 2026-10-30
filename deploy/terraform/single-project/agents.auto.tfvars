# Container image for the agent tier (Cloud Run MCP servers + HumanQueue; GKE FaqResolver uses the same image).
# Bump this after each services/cloudbuild.yaml build; committed so plans stay stable.
agents_image                     = "us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:308dfaa"
enable_re_service_agent_bindings = true
# REQ-AUTH-003: order-lookup requires a verified customer context token (flipped from accept after live e2e 5/5).
cct_mode                         = "require"
# REQ-AUTH-004: SPIFFE agent identity of the PRODUCTION TriageRouter engine (2170218524805431296, promoted from the staging pilot 2026-10-30).
triage_agent_principal           = "principal://agents.global.org-349636765856.system.id.goog/resources/aiplatform/projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
# REQ-AGENT-031: human-queue Cloud Run URL for Pub/Sub push subscription and redaction-purge
human_queue_url                  = "https://human-queue-34mwh7k34a-uc.a.run.app"
# REQ-AUTH-007: egress Agent Gateway bound to the TriageRouter engine by CD (applied from deploy/terraform/agent-gateway).
triage_agent_gateway             = "projects/susmith-fde-ticket-router/locations/us-central1/agentGateways/tr-egress"
# REQ-AUTH-008/009: FaqResolver GKE Agent Identity (SPIFFE) principal and order-lookup caller allowlist.
faq_agent_principal              = "principal://agents.global.org-349636765856.system.id.goog/resources/container/projects/620485621180/locations/us-central1/clusters/support-agents/ns/faq-resolver/sa/ksa-faq"
# Enforcement is switched on after the FaqResolver pod is verified to present its SPIFFE ID (REQ-AUTH-009 staged rollout):
# allowed_caller_spiffe_ids      = "spiffe://agents.global.org-349636765856.system.id.goog/resources/container/projects/620485621180/locations/us-central1/clusters/support-agents/ns/faq-resolver/sa/ksa-faq"
allowed_caller_spiffe_ids        = ""
