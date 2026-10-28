# Continuous Deployment (CD) Operations Runbook

This runbook covers operations for the human-approved Continuous Deployment (CD) pipeline managing the Support Ticket Router multi-agent system (Cloud Run MCP services, GKE Autopilot FaqResolver, and Vertex AI Agent Engine TriageRouter).

---

## 1. Architectural Invariants & Ownership Boundary (REQ-CD-010)

This repository maintains a strict operational separation between infrastructure and application code:

1. **Infrastructure Ownership (Terraform)**:
   - Infrastructure resources (VPC networks, Cloud SQL, GKE cluster, Pub/Sub topics, Cloud Run service configs, IAM roles, Agent Search engines, Secret Manager) are owned exclusively by Terraform.
   - **HARD INVARIANT**: Terraform MUST NEVER be installed or executed on the local Cloudtop workstation. All Terraform operations execute solely on Workbench VM `laya-tune` via `./scripts/tf_vm.sh`.
   - Cloud Run services declare `lifecycle { ignore_changes = [template[0].containers[0].image, client, client_version] }`. The Terraform `agents_image` variable is used only as a bootstrap image during initial service creation; subsequent image updates are owned entirely by the CD pipeline.

2. **Application Release Ownership (CD Pipeline)**:
   - Application container images and Agent Engine code versions are owned exclusively by the automated CD pipeline (`cicd/cloudbuild-deploy.yaml`).
   - The CD pipeline never runs `terraform` commands and mutates only runtime versions.
   - The pipeline executes under the least-privilege service account `sa-cd-deployer@susmith-fde-ticket-router.iam.gserviceaccount.com` (REQ-CD-009).

---

## 2. Approving a Production Deployment (REQ-CD-001)

When changes merge to the `main` branch, the Cloud Build trigger `support-ticket-router-cd` is triggered in a **PENDING_APPROVAL** state.

### Option A: Via Google Cloud Console
1. Navigate to **Cloud Build** > **Build history** in the GCP Console for project `susmith-fde-ticket-router` (region `us-central1`).
2. Locate the pending build for trigger `support-ticket-router-cd`.
3. Review the commit message, diff, and associated CI status.
4. Click **Approve** to execute the deployment.

### Option B: Via gcloud CLI
Authorized approvers (`var.cd_approvers`, default `user:susmith@gcp.altostrat.com`) can approve the build from the command line:

```bash
# List builds waiting for approval
gcloud builds list \
  --region us-central1 \
  --project susmith-fde-ticket-router \
  --filter="status=PENDING_APPROVAL" \
  --format="table(id,createTime,substitutions.SHORT_SHA,status)"

# Approve the pending deployment build
gcloud builds approve <BUILD_ID> \
  --region us-central1 \
  --project susmith-fde-ticket-router
```

---

## 3. Pipeline Stages & Verification (REQ-CD-002 – REQ-CD-007)

Once approved, the pipeline executes the following stages:

1. **`require-ci-green` (REQ-CD-002)**:
   - Polls `gcloud builds list` for the same git commit (`$SHORT_SHA`) on trigger `support-ticket-router-ci`.
   - Fails immediately if CI failed, timed out, or was cancelled. Capped at 30 minutes.

2. **`build-and-push-image` (REQ-CD-003)**:
   - Builds the unified multi-service container image from `services/Dockerfile`.
   - Tags and pushes `us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:$SHORT_SHA`.

3. **`fetch-last-good` (REQ-CD-008)**:
   - Downloads previous deployment state from `gs://susmith-fde-ticket-router-laya/cd/last_good.json`.
   - Generates `/workspace/rollback.sh` trap handler.

4. **`deploy-cloud-run` (REQ-CD-004)**:
   - Updates `ticket-db-mcp`, `order-lookup-mcp`, and `human-queue` with the new container image.
   - Waits for healthy revisions across all three microservices.

5. **`deploy-gke` (REQ-CD-005)**:
   - Connects to GKE Autopilot cluster `support-agents` via the DNS endpoint (`--dns-endpoint`).
   - Renders manifests in `deploy/k8s/faq-resolver/` with the target image tag.
   - Applies manifests with `kubectl apply` and waits for rollout (`rollout status deployment/faq-resolver --timeout=300s`).

6. **`deploy-agent-engine` (REQ-CD-006)**:
   - Installs the `deploy` optional extra (`pip install -e .[deploy]`).
   - Verifies the target reasoning engine is not `UPDATING` (polling ≤10 min).
   - Executes `scripts/deploy_agents/deploy_triage_agent_runtime.py --update <RESOURCE_NAME>`.

7. **`post-deploy-smoke` (REQ-CD-007)**:
   - **Cloud Run MCP Smoke**: `scripts/smoke/cloud_run_smoke.py` (/health 200, unauth 403, idempotent upsert dedup, customer boundary 404, body customer_id 400).
   - **Identity & Authorization Suite**: `scripts/e2e_identity_check.py` (live token auth, BOLA isolation, unauthenticated escalation, boundary ack).
   - **Model Armor Sanitization**: `scripts/model_armor_smoke.py --timeout-s 5` (functional PI/JB and RAI filter verification).

8. **`record-last-good` (REQ-CD-008)**:
   - On full success, writes `{sha, image, build_id, time}` to `gs://susmith-fde-ticket-router-laya/cd/last_good.json`.

---

## 4. Rollback Procedures (REQ-CD-008)

### Automatic Rollback
If any deployment or smoke test step fails, the bash `ERR` trap executes `/workspace/rollback.sh`:
1. Reads previous good image from `/workspace/last_good.json`.
2. Reverts Cloud Run services (`ticket-db-mcp`, `order-lookup-mcp`, `human-queue`) to the last-good image.
3. Reverts GKE deployment (`faq-resolver`) to the last-good image.
4. Outputs the exact Agent Engine rollback command.
5. Fails the build with exit code 1.

### Re-running Deployment on Last-Good SHA
To restore the entire 3-runtime deployment stack (including Vertex AI Agent Engine) to the last known good release:

```bash
# 1. Inspect the last known good commit and image
gsutil cat gs://susmith-fde-ticket-router-laya/cd/last_good.json

# Sample output:
# {
#   "sha": "a1d1fa5",
#   "image": "us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:a1d1fa5",
#   "build_id": "3e9b14f8-1234-5678-9abc-def012345678",
#   "time": "2026-10-28T02:00:00Z"
# }

# 2. Trigger the CD pipeline directly on the last-good SHA
gcloud builds triggers run support-ticket-router-cd \
  --region us-central1 \
  --project susmith-fde-ticket-router \
  --sha=<LAST_GOOD_SHA>

# 3. Approve the rollback deployment build
gcloud builds approve <ROLLBACK_BUILD_ID> \
  --region us-central1 \
  --project susmith-fde-ticket-router
```

---

## 5. Break-Glass Emergency Rollback (Manual Direct Commands)

If Cloud Build itself is degraded or inaccessible, manual rollback commands can be run by an authorized operator:

### 1. Cloud Run Direct Rollback
```bash
LAST_IMAGE="us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:<LAST_GOOD_SHA>"

for svc in ticket-db-mcp order-lookup-mcp human-queue; do
  gcloud run services update "$svc" \
    --image "${LAST_IMAGE}" \
    --region us-central1 \
    --project susmith-fde-ticket-router \
    --quiet
done
```

### 2. GKE Direct Rollback
```bash
gcloud container clusters get-credentials support-agents \
  --region us-central1 \
  --project susmith-fde-ticket-router \
  --dns-endpoint

kubectl set image deployment/faq-resolver faq-resolver="${LAST_IMAGE}" -n faq-resolver
kubectl rollout status deployment/faq-resolver -n faq-resolver --timeout=300s
```

### 3. Agent Engine Direct Update
From an environment with the repository and ADC credentials:
```bash
git checkout <LAST_GOOD_SHA>
pip install -e .[deploy]
python scripts/deploy_agents/deploy_triage_agent_runtime.py \
  --update "projects/620485621180/locations/us-central1/reasoningEngines/8384262420809383936" \
  --project susmith-fde-ticket-router \
  --region us-central1
```

## Change-aware Agent Engine update (REQ-CD-011)

- CD hashes the TriageRouter package (`scripts/deploy_agents/ae_fingerprint.py compute`) and compares it with
  `gs://susmith-fde-ticket-router-laya/cd/agent_engine_deployed.json` (written right after each successful update).
  Match → the Agent Engine step logs `SKIP:` and exits in seconds; mismatch or missing record → `DEPLOY:`.
- FAQ, HumanQueue, MCP, store, retrieval, docs, eval and CI-only changes do not change the fingerprint.
- Force an update (e.g. after a manual console change): run the trigger with `_FORCE_AGENT_ENGINE=true`.
- Check what's live: `gcloud storage cat gs://susmith-fde-ticket-router-laya/cd/agent_engine_deployed.json`.
