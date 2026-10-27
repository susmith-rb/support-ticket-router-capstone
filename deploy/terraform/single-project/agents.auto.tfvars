# Container image for the agent tier (Cloud Run MCP servers + HumanQueue; GKE FaqResolver uses the same image).
# Bump this after each services/cloudbuild.yaml build; committed so plans stay stable.
agents_image = "us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:c382e93"
