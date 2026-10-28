# CD Pipeline (human-approved continuous deployment)

Status: approved by conductor 2026-10-28. Owner: platform.

Moves agent **image/code versions** to all three runtimes after a human approves the
build. Terraform still owns infrastructure and runs only on laya-tune (user rule);
CD never runs `terraform`.

| ID | Requirement | Status | Acceptance |
|---|---|---|---|
| `REQ-CD-001` | Approval-gated trigger on push to main | done | `tests/infra/test_cd_pipeline_static.py::test_cd_trigger_requires_approval` |
| `REQ-CD-002` | CI-green gate for the same commit | done | `tests/infra/test_cd_pipeline_static.py::test_cd_requires_ci_green` |
| `REQ-CD-003` | Build + push `agents:$SHORT_SHA` once | done | `tests/infra/test_cd_pipeline_static.py::test_cd_builds_single_image` |
| `REQ-CD-004` | Deploy Cloud Run (3 services) | done | `tests/infra/test_cd_pipeline_static.py::test_cd_deploys_cloud_run` |
| `REQ-CD-005` | Deploy FaqResolver to GKE | done | `tests/infra/test_cd_pipeline_static.py::test_cd_deploys_gke` |
| `REQ-CD-006` | Update TriageRouter on Agent Engine | done | `tests/infra/test_cd_pipeline_static.py::test_cd_updates_agent_engine` |
| `REQ-CD-007` | Post-deploy smoke gates | done | `tests/infra/test_cd_pipeline_static.py::test_cd_post_deploy_smoke` |
| `REQ-CD-008` | Rollback + last-good record | done | `tests/infra/test_cd_pipeline_static.py::test_cd_rollback_and_last_good` |
| `REQ-CD-009` | Least-privilege CD service account (Terraform) | done | `tests/infra/test_cd_pipeline_static.py::test_cd_service_account_roles` |
| `REQ-CD-010` | Terraform/CD ownership split (no drift fights) | done | `tests/infra/test_cd_pipeline_static.py::test_terraform_ignores_cd_owned_fields` |

---

### REQ-CD-001: Approval-gated trigger on push to main
- **Statement**: Cloud Build trigger `support-ticket-router-cd` (module `cloudbuild`) MUST execute `cicd/cloudbuild-deploy.yaml` upon push to `^main$` gated by manual approval (`approval_config.approval_required = true`), executing as service account `sa-cd-deployer`, enabled via `enable_cd_trigger` (default `true`), with approver IAM `roles/cloudbuild.builds.approver` granted to `var.cd_approvers`.
- **Rationale**: TDD §12.6: Production deployments across three agent runtimes require mandatory human verification and sign-off before artifacts are rolled out.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_trigger_requires_approval`
- **Status**: done
- **Owner**: platform

- `google_cloudbuild_trigger.cd` (module `cloudbuild`), name `support-ticket-router-cd`, push to `^main$`, `filename = "cicd/cloudbuild-deploy.yaml"`, `approval_config { approval_required = true }`, `service_account = sa-cd-deployer`, enabled by var `enable_cd_trigger` (default `true`). Approvers: `roles/cloudbuild.builds.approver` for `var.cd_approvers` (default `["user:susmith@gcp.altostrat.com"]`).
- Approve with `gcloud builds approve <BUILD_ID> --region us-central1` or the GCP Console.

---

### REQ-CD-002: CI-green gate for the same commit
- **Statement**: The first step of `cicd/cloudbuild-deploy.yaml` MUST poll Cloud Build builds for commit `${SHORT_SHA}` on trigger `_CI_TRIGGER_ID` until `SUCCESS`, failing the pipeline immediately upon `FAILURE`, `TIMEOUT`, or `CANCELLED`, capped at a 30-minute deadline.
- **Rationale**: TDD §12.6: No deployment to serving infrastructure may occur without prior verification of tests, lint, and retrieval evaluation gates.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_requires_ci_green`
- **Status**: done
- **Owner**: platform

- First step polls `gcloud builds list --region us-central1 --filter="substitutions.SHORT_SHA=$SHORT_SHA AND buildTriggerId=<ci trigger id>"` (trigger id passed as substitution `_CI_TRIGGER_ID` from Terraform) until a CI build for the same SHA is `SUCCESS` (fail on FAILURE/TIMEOUT/CANCELLED, 30 min cap). No deploy without green CI.

---

### REQ-CD-003: Build and push unified container image once
- **Statement**: The CD pipeline MUST build the unified container image once using `services/Dockerfile` tagged as `us-central1-docker.pkg.dev/$PROJECT_ID/serving/agents:$SHORT_SHA` and push it to Artifact Registry, with all runtime targets deploying this exact digest tag.
- **Rationale**: REQ-AGENT-001, REQ-AGENT-010: Hermetic container deployments across all runtimes require reproducible, single-build container artifacts.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_builds_single_image`
- **Status**: done
- **Owner**: platform

- `docker build -f services/Dockerfile -t us-central1-docker.pkg.dev/$PROJECT_ID/serving/agents:$SHORT_SHA .` and push. Every runtime deploys this exact tag.

---

### REQ-CD-004: Deploy Cloud Run microservices
- **Statement**: The CD pipeline MUST update Cloud Run services `ticket-db-mcp`, `order-lookup-mcp`, and `human-queue` with the new image tag in region `us-central1`, waiting for a ready revision while preserving environment variables and IAM policies managed by Terraform.
- **Rationale**: REQ-AGENT-017, TDD §12.6: Microservice revision lifecycle updates container images independently of infrastructure declarations.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_deploys_cloud_run`
- **Status**: done
- **Owner**: platform

- `gcloud run services update {ticket-db-mcp,order-lookup-mcp,human-queue} --image <tag> --region us-central1` (image only; env/IAM stay in Terraform). Wait for ready revision.

---

### REQ-CD-005: Deploy FaqResolver to GKE via DNS endpoint
- **Statement**: The CD pipeline MUST connect to GKE cluster `support-agents` using the IAM-authenticated DNS endpoint (`--dns-endpoint`), render manifests in `deploy/k8s/faq-resolver/` with the commit image tag, apply them via `kubectl apply`, and wait for `deployment/faq-resolver` rollout status up to 300s.
- **Rationale**: REQ-AGENT-019: Private-endpoint cluster requires DNS-based control plane endpoint for direct, secure API access without public IP allowlisting.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_deploys_gke`
- **Status**: done
- **Owner**: platform

- Cluster control plane is private-endpoint-enforced; CD reaches it via the **DNS-based endpoint** (`gcloud container clusters get-credentials support-agents --region us-central1 --dns-endpoint`), which requires Terraform `control_plane_endpoints_config.dns_endpoint_config.allow_external_traffic = true` (IAM-gated; no IP allowlist).
- `kubectl apply -f deploy/k8s/faq-resolver/` with the image field rendered to `<tag>` (sed on a copy; manifests in repo stay the source of truth for env), then `kubectl rollout status deployment/faq-resolver -n faq-resolver --timeout=300s`.

---

### REQ-CD-006: Update TriageRouter on Vertex AI Agent Engine
- **Statement**: The CD pipeline MUST install the `deploy` optional extra (`pip install -e .[deploy]`), verify that the target reasoning engine is not in an `UPDATING` state (polling up to 10 minutes), and invoke `scripts/deploy_agents/deploy_triage_agent_runtime.py --update <resource>`.
- **Rationale**: REQ-AGENT-017: Agent Runtime updates must prevent concurrent mutation 400 errors and serialize agent closures with pinned dependencies.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_updates_agent_engine`
- **Status**: done
- **Owner**: platform

- Step on `python:3.11-slim`: `pip install -e .[deploy]` (add a `deploy` extra to pyproject with the exact pins the deploy script already uses), then `python scripts/deploy_agents/deploy_triage_agent_runtime.py --update <resource>` (resource from substitution `_TRIAGE_RESOURCE`). Before calling update, wait until the engine is not `UPDATING` (poll ≤10 min) — a concurrent update returns 400.

---

### REQ-CD-007: Post-deploy smoke verification gates
- **Statement**: The CD pipeline MUST execute three post-deploy smoke verification scripts in sequence, failing closed if any check fails: (1) `scripts/smoke/cloud_run_smoke.py`, (2) `scripts/e2e_identity_check.py`, and (3) `scripts/model_armor_smoke.py --timeout-s 5`.
- **Rationale**: REQ-IDENT-008, REQ-MA-006: Immediate end-to-end verification of service health, authentication boundaries, BOLA isolation, and Model Armor security controls is required before declaring a deployment successful.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_post_deploy_smoke`
- **Status**: done
- **Owner**: platform

1. `scripts/smoke/cloud_run_smoke.py` (port of the operator smoke: /health 200 on 3 services, unauthenticated 403, idempotent upsert dedup, body-customer_id rejected 400).
2. `scripts/e2e_identity_check.py` (demo tokens from Secret Manager `identity-demo-user-1001/1002`, `identity-web-api-key`).
3. `scripts/model_armor_smoke.py --timeout-s 5` (functional 5/5).

---

### REQ-CD-008: Rollback on failure and last-good record on success
- **Statement**: On complete success, the pipeline MUST write `{sha, image, build_id, time}` to `gs://$PROJECT_ID-laya/cd/last_good.json`. On any deployment or smoke failure, the pipeline MUST roll back Cloud Run and GKE deployments to the image recorded in `last_good.json`, output instructions for Agent Engine re-run, and exit with code 1.
- **Rationale**: TDD §12.6: Automatic rollback minimizes blast radius and downtime for customer-facing services upon bad deployments.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_rollback_and_last_good`
- **Status**: done
- **Owner**: platform

- On success write `gs://$PROJECT_ID-laya/cd/last_good.json` `{sha, image, build_id, time}`.
- If any deploy or smoke step fails: roll Cloud Run + GKE back to `last_good.image`, then fail the build. Agent Engine rollback = re-run the CD trigger on the last-good SHA (`gcloud builds triggers run support-ticket-router-cd --sha=<sha>`), documented in the runbook; the build log prints that command.

---

### REQ-CD-009: Least-privilege CD service account
- **Statement**: The CD pipeline service account (`sa-cd-deployer@${PROJECT_ID}.iam.gserviceaccount.com`) MUST be granted only minimal required deployment roles without project Owner or Editor: `roles/run.developer`, `roles/container.developer`, `roles/aiplatform.user`, `roles/artifactregistry.writer` (serving repo), `roles/cloudbuild.builds.viewer`, `roles/logging.logWriter`, `roles/storage.objectAdmin` (corpus/laya bucket), `roles/secretmanager.secretAccessor` (3 identity secrets), `roles/modelarmor.user`, `roles/run.invoker` (3 Cloud Run services), and `roles/iam.serviceAccountUser` strictly on runtime service accounts.
- **Rationale**: TDD §12.6, REQ-CD-009: Least privilege security boundary prevents privilege escalation from automated build environments.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_cd_service_account_roles`
- **Status**: done
- **Owner**: platform

- `sa-cd-deployer@`: `roles/run.developer`, `roles/container.developer`, `roles/aiplatform.user`, `roles/artifactregistry.writer` (serving repo), `roles/cloudbuild.builds.viewer`, `roles/logging.logWriter`, `roles/storage.objectAdmin` on the laya bucket (staging + last_good), `roles/secretmanager.secretAccessor` on the 3 identity secrets only, `roles/modelarmor.user`, `roles/run.invoker` on the 3 services (smoke), and `roles/iam.serviceAccountUser` **only** on the runtime SAs it deploys as (Cloud Run SAs, sa-triage). No owner/editor.

---

### REQ-CD-010: Terraform and CD ownership split
- **Statement**: Terraform Cloud Run definitions MUST declare `lifecycle { ignore_changes = [template[0].containers[0].image, client, client_version] }`, allowing the CD pipeline to mutate active container images without triggering configuration drift on subsequent Terraform plans.
- **Rationale**: TDD §12.6: Clear ownership split ensures infrastructure changes (Terraform on VM) and artifact releases (CD pipeline) never conflict or overwrite each other.
- **Acceptance**: `tests/infra/test_cd_pipeline_static.py::test_terraform_ignores_cd_owned_fields`
- **Status**: done
- **Owner**: platform

- Terraform Cloud Run services: `lifecycle { ignore_changes = [template[0].containers[0].image, client, client_version] }`; `agents_image` var remains the bootstrap image for first create only.
- Runbook `docs/runbooks/deploy.md`: infra = Terraform on laya-tune; versions = CD. Manual operator deploy scripts are removed once CD is verified live.
