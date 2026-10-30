# Agent Gateway Egress Governance (REQ-AUTH-007)

This directory defines the standalone Terraform root for **Agent Gateway** egress governance (`tr-egress`), governing outbound calls from the TriageRouter Agent Runtime engine to FaqResolver (A2A handoff) and core Google APIs.

- **Providers**: `hashicorp/google` and `hashicorp/google-beta` `~> 7.20`
- **State prefix**: `gs://susmith-fde-ticket-router-tfstate/terraform/state/agent-gateway/`
- **Access model**: IAM v3 Unified Access Policy (UAP) allowing ONLY the TriageRouter SPIFFE agent identity (`iap.googleapis.com/resources.egressViaIAP`) to the registered FaqResolver A2A agent card and composite `core-gapi` endpoints.

---

## Deployment & Apply Order

All Terraform commands must execute on the Workbench VM `laya-tune` via `scripts/tf_vm.sh`. Never run Terraform directly on the cloudtop.

### Step 1: Sync code to VM
```bash
./scripts/tf_vm.sh sync
```

### Step 2: Initialize & Validate Root
```bash
./scripts/tf_vm.sh run agent-gateway init
./scripts/tf_vm.sh validate agent-gateway
```

### Step 3: Apply in DRY_RUN Mode (Default)
In `DRY_RUN` mode, the IAP authz extension evaluates traffic against policy and emits audit log decisions without dropping or blocking any calls.
```bash
./scripts/tf_vm.sh logged-run agent-gateway agw_plan_dryrun plan
./scripts/tf_vm.sh run agent-gateway apply -auto-approve
```

### Step 4: Bind TriageRouter Engine to Gateway
Deploy or update the TriageRouter Agent Engine instance with `--identity-type agent` and `--agent-gateway`:
```bash
python scripts/deploy_agents/deploy_triage_agent_runtime.py \
  --identity-type agent \
  --resource-name "projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296" \
  --agent-gateway "projects/620485621180/locations/us-central1/agentGateways/tr-egress"
```

### Step 5: Watch Gateway Logs
Inspect egress audit logs to verify that all TriageRouter traffic is marked as `ALLOWED` and that no unexpected 403 or 498 status codes occur:
```bash
gcloud logging read 'resource.type="networkservices.googleapis.com/Gateway" AND resource.labels.gateway_name="tr-egress"' \
  --limit 50 --format json
```

### Step 6: Set ENFORCE Mode and Re-apply
Once logs confirm 100% clean policy evaluations, enforce the gateway access control:
```bash
./scripts/tf_vm.sh run agent-gateway apply -var="enforcement_mode=ENFORCE" -auto-approve
```

Verify with post-deploy smoke / e2e check:
```bash
python scripts/e2e_identity_check.py \
  --resource-name "projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296" \
  --project "susmith-fde-ticket-router" \
  --region "us-central1"
```

---

## Rollback Procedure

If egress issues occur or calls are rejected:

### 1. Revert Gateway to DRY_RUN Mode
```bash
./scripts/tf_vm.sh run agent-gateway apply -var="enforcement_mode=DRY_RUN" -auto-approve
```

### 2. Unbind Gateway from Agent Engine
Clear the `agentGatewayConfig` on the Reasoning Engine via REST PATCH:
```bash
TRIAGE_RESOURCE="projects/620485621180/locations/us-central1/reasoningEngines/2170218524805431296"
ACCESS_TOKEN="$(CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false gcloud auth print-access-token)"

curl -X PATCH \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"spec": {"deploymentSpec": {"agentGatewayConfig": {}}}}' \
  "https://us-central1-aiplatform.googleapis.com/v1beta1/${TRIAGE_RESOURCE}?updateMask=spec.deploymentSpec.agentGatewayConfig"
```
