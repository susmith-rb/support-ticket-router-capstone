# Container image for the agent tier (Cloud Run MCP servers + HumanQueue; GKE FaqResolver uses the same image).
# Bump this after each services/cloudbuild.yaml build; committed so plans stay stable.
agents_image                     = "us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:b255d0d"
enable_re_service_agent_bindings = true
# REQ-AUTH-003: order-lookup requires a verified customer context token (flipped from accept after live e2e 5/5).
cct_mode = "require"
# REQ-AUTH-004: SPIFFE agent identity of the PRODUCTION TriageRouter engine (2170218524805431296, promoted from the staging pilot 2026-10-30).
triage_agent_principal = "principal://agents.global.org-349636765856.system.id.goog/resources/aiplatform/projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
# REQ-AGENT-031: human-queue Cloud Run URL for Pub/Sub push subscription and redaction-purge
human_queue_url = "https://human-queue-34mwh7k34a-uc.a.run.app"
# REQ-AUTH-007: egress Agent Gateway bound to the TriageRouter engine by CD (applied from deploy/terraform/agent-gateway).
triage_agent_gateway = "projects/susmith-fde-ticket-router/locations/us-central1/agentGateways/tr-egress"
# REQ-AUTH-008/009: FaqResolver GKE Agent Identity (SPIFFE) principal and order-lookup caller allowlist.
faq_agent_principal = "principal://agents.global.org-349636765856.system.id.goog/resources/container/projects/620485621180/locations/us-central1/clusters/support-agents/ns/faq-resolver/sa/ksa-faq-agent"
# REQ-AUTH-009 enforced 2026-10-31: FaqResolver SPIFFE caller allowed, other run.invoker callers get 403 caller_not_authorized.
allowed_caller_spiffe_ids = "spiffe://agents.global.org-349636765856.system.id.goog/resources/container/projects/620485621180/locations/us-central1/clusters/support-agents/ns/faq-resolver/sa/ksa-faq-agent"

# REQ-AUTH-008: FaqResolver runs as ksa-faq-agent with GKE Agent Identity. Canary 2026-10-31 01:07
# verified search, Model Armor, Pub/Sub publish, and SPIFFE ID tokens accepted by order-lookup
# and ticket-db-mcp. Rollback: "0" (CD re-applies workload-identity.patch.yaml).
faq_agent_identity = "1"
