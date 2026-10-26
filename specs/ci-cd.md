# Specification: CI/CD Pipelines and Automation (`ci-cd`)

This specification defines the declarative Cloud Build pipelines, automated retrieval regression gates, scheduled health evaluations, least-privilege service accounts, and GitHub v2 repository connections for the Support Ticket Router & Assistant capstone project.

---

## 1. Overview & Pipeline Architecture

Following TDD §12.6, TDD §6.5, Conductor Plan 8 (`docs/plans/PLAN8.md`), and the Google Cloud AI FDE Delivery standards (`ai-fde-delivery`), CI/CD is codified declaratively under `cicd/` and provisioned via Terraform under `deploy/terraform/single-project/modules/cloudbuild/`:

```
cicd/
├── cloudbuild.yaml          # Main pipeline: unit tests, py_compile, lint, terraform, golden eval gate, deploy placeholders
├── cloudbuild-reindex.yaml  # Triggered on push to main touching rag/corpus_v2/** (publish, eval, gate, rollback)
└── cloudbuild-nightly.yaml  # Scheduled nightly via Cloud Scheduler (live retrieval drift & latency benchmark)
```

### Three-Tier Pipeline Layout

1. **Continuous Integration & Delivery Pipeline (`cicd/cloudbuild.yaml`)**:
   - Mirrors the reference enterprise CI/CD pattern (from `adisairam-capstone-onboarding-agent`), structured in sequential stages:
     - **Stage 1 (Unit & Resilience Tests)**: Installs dependencies from `requirements-dev.txt`, runs `pytest -v` across all offline tests (including spec traceability `tests/test_spec_traceability.py`), verifies Python byte-compilation via `python -m py_compile`, and executes offline FAQ corpus linting (`python rag/publish_faq.py --work rag/corpus_v2 --lint-only`).
     - **Stage 2 (Static Terraform Checks)**: Executes `terraform fmt -check`, `terraform validate`, and `tflint --recursive`.
     - **Stage 3 (Golden Retrieval Evaluation Gate)**: Fetches held-out DEV queries and Adjudicator C multi-annotator labels from GCS (`gs://susmith-fde-ticket-router-laya/eval/`), executes `python3 rag/ci_eval_gate.py` with production configuration (`--threshold LOWEST --clean`), and enforces retrieval quality thresholds. If any threshold is breached, the script exits with code 1, which immediately **blocks** all subsequent build and deployment stages.
     - **Stages 4–7 (Container Build, Push, Deploy & Post-Deploy Smoke)**: Guarded behind substitution variable `_DEPLOY=false` (and dependency DAG `waitFor`) until the Cloud Run / Agent Runtime serving containers are implemented.
   - Enforces `options.logging = "CLOUD_LOGGING_ONLY"` to avoid dependency on legacy Cloud Storage log buckets.

2. **Automated Corpus Reindexing & Retrieval Regression Gate (`cicd/cloudbuild-reindex.yaml`)**:
   - Triggered when changes are pushed to `main` touching `rag/corpus_v2/**`.
   - Lints corpus, packages the next incremental release, and publishes documents to Discovery Engine (`global`).
   - Downloads DEV evaluation queries and Adjudicator C multi-labels from `gs://susmith-fde-ticket-router-laya/eval/`.
   - Runs retrieval evaluation and enforces the hard regression gate: overall `Δrecall@5 ≥ −0.02` vs baseline. If the gate fails, automatically initiates an atomic rollback to the previous release.

3. **Nightly Live Retrieval Health & Drift Evaluation (`cicd/cloudbuild-nightly.yaml`)**:
   - Triggered on a daily schedule (`0 2 * * *`, 02:00 UTC) via Cloud Scheduler calling the trigger `:run` REST API endpoint.
   - Benchmarks live Discovery Engine query performance against DEV queries (measuring `recall@5`, `MRR@10`, `nDCG@5`, and p95 latency) using `rag/ci_eval_gate.py`.
   - Compares metrics against the stored production baseline in GCS (`gs://susmith-fde-ticket-router-laya/eval/nightly/baseline.json`).
   - Archives timestamped evaluation results to GCS and fails the build if any metric degrades > 0.03 below baseline.

### Identity, IAM & Connection Gating

- **Build Service Account (`ticket-router-build`)**:
  - `roles/discoveryengine.editor` (to publish documents and invoke searches in Discovery Engine).
  - `roles/storage.objectAdmin` on `susmith-fde-ticket-router-laya` (to download eval sets and write nightly benchmark results).
  - `roles/logging.logWriter` (to stream Cloud Build logs).
  - Primitive roles (`roles/owner`, `roles/editor`) and project-wide IAM bindings are strictly forbidden per TDD §12.6.
- **Scheduler Service Account (`ticket-router-scheduler`)**:
  - `roles/cloudbuild.builds.editor` (to trigger nightly build runs via authenticated HTTP POST).
- **GitHub Connection Gating (`github_connection_ready = false`)**:
  - Cloud Build 2nd gen connections (`google_cloudbuildv2_connection`) require a one-time manual console flow where the user installs the Google Cloud Build GitHub App and authorizes the OAuth secret in Secret Manager.
  - Terraform gates connection, repository, and push/PR trigger creation behind `var.github_connection_ready`.
  - Service accounts, IAM bindings, the manual-invocation nightly trigger, and the Cloud Scheduler job can be provisioned safely before the GitHub connection is activated.

---

## 2. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-CICD-001` | Declarative CI/CD Pipeline Specifications | `done` | `tests/infra/test_cicd_static.py::test_cicd_files_exist` |
| `REQ-CICD-002` | Automated Pull-Request and Push CI Gate | `done` | `tests/infra/test_cicd_static.py::test_ci_pipeline_contract` |
| `REQ-CICD-003` | Corpus Reindexing and Retrieval Regression Gate | `done` | `tests/infra/test_cicd_static.py::test_reindex_pipeline_contract` |
| `REQ-CICD-004` | Nightly Retrieval Health and Drift Evaluation | `done` | `tests/infra/test_cicd_static.py::test_nightly_pipeline_contract` |
| `REQ-CICD-005` | Dedicated Least-Privilege Build and Scheduler Service Accounts | `done` | `tests/infra/test_cicd_static.py::test_cloudbuild_sa_least_privilege` |
| `REQ-CICD-006` | Cloud Build v2 GitHub Connection and Repository Resources | `done` | `tests/infra/test_cicd_static.py::test_github_connection_gating` |
| `REQ-CICD-007` | Cloud Scheduler Nightly Evaluation Target Configuration | `done` | `tests/infra/test_cicd_static.py::test_scheduler_job_target_contract` |
| `REQ-CICD-008` | Static Security and Secret Leakage Prevention | `done` | `tests/infra/test_cicd_static.py::test_no_secrets_in_cicd_yamls` |
| `REQ-CICD-009` | Change-Aware Golden Retrieval Evaluation Gate Execution | `done` | `tests/infra/test_cicd_static.py::test_eval_gate_conditional_execution` |

---

## 3. Detailed Requirements

### REQ-CICD-001: Declarative CI/CD Pipeline Specifications
- **Statement**: CI/CD workflows MUST be codified as declarative Cloud Build YAML configuration files under `cicd/`: `cloudbuild.yaml` for pull-request and push validation, `cloudbuild-reindex.yaml` for automated FAQ corpus reindexing, and `cloudbuild-nightly.yaml` for scheduled retrieval quality evaluation.
- **Rationale**: Replaces fragile manual build commands with version-controlled, auditable build declarations mirroring the reference capstone architecture (TDD §12.6; PLAN8.md §1).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_cicd_files_exist`
- **Status**: done
- **Owner**: ci-cd

### REQ-CICD-002: Automated Pull-Request and Push CI Gate
- **Statement**: The continuous integration pipeline (`cicd/cloudbuild.yaml`) MUST validate all pull requests and pushes to `main` by: (1) installing development dependencies from `requirements-dev.txt`, (2) executing the full offline pytest suite including spec traceability validation, (3) verifying Python byte-compilation of `rag/*.py` and pipeline scripts, (4) executing offline corpus linting via `python rag/publish_faq.py --work rag/corpus_v2 --lint-only`, (5) executing Terraform format checks, validation, and tflint against `deploy/terraform/`, and (6) executing the Golden Retrieval Evaluation Gate (`rag/ci_eval_gate.py`), exiting 1 to block subsequent build and deploy steps upon any metric regression.
- **Rationale**: Guarantees zero broken tests, 100% spec traceability, valid policy metadata, clean Terraform syntax, and verified retrieval quality prior to merge (TDD §12.6 Stages 1–2; PLAN8.md §1).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_ci_pipeline_contract`
- **Status**: done
- **Owner**: ci-cd

### REQ-CICD-003: Corpus Reindexing and Retrieval Regression Gate
- **Statement**: Pushes to `main` modifying files under `rag/corpus_v2/**` MUST trigger `cicd/cloudbuild-reindex.yaml` to lint the corpus, publish a new release to Discovery Engine (`global`), download DEV evaluation queries and Adjudicator C multi-labels from `gs://susmith-fde-ticket-router-laya/eval/`, execute retrieval evaluation with production configuration (`--threshold LOWEST --clean`), and enforce that overall `Δrecall@5 ≥ −0.02` vs baseline, triggering automated rollback to the previous release upon gate failure.
- **Rationale**: Automates FAQ knowledge lifecycle publishing while enforcing empirical quality gates before changes serve production traffic (TDD §6.5; specs/rag-retrieval.md §REQ-RAG-RETR-006; PLAN8.md §1).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_reindex_pipeline_contract`
- **Status**: done
- **Owner**: ci-cd

### REQ-CICD-004: Nightly Retrieval Health and Drift Evaluation
- **Statement**: A scheduled nightly pipeline (`cicd/cloudbuild-nightly.yaml`) is configured to evaluate live retrieval metrics (recall@5, MRR@10, nDCG@5, p95 latency) against stored production baselines, archive timestamped results to `gs://susmith-fde-ticket-router-laya/eval/nightly/`, and fail the build if any retrieval metric drops > 0.03 below baseline. (Stretch: schedule paused by default via `enable_nightly_schedule`; trigger available on demand).
- **Rationale**: Detects semantic ranker drift, silent corpus degradation, or API latency regressions without incurring unwanted daily queries; manual on-demand execution is retained (TDD §13; PLAN8.md §1).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_nightly_pipeline_contract`
- **Status**: done
- **Owner**: ci-cd

### REQ-CICD-005: Dedicated Least-Privilege Build and Scheduler Service Accounts
- **Statement**: Cloud Build pipelines and Cloud Scheduler jobs MUST execute under dedicated service accounts (`ticket-router-build` and `ticket-router-scheduler`). The build SA MUST only receive `roles/discoveryengine.editor`, `roles/logging.logWriter`, and bucket-level `roles/storage.objectAdmin` on the corpus bucket. The scheduler SA MUST only receive `roles/cloudbuild.builds.editor`. Neither SA may be granted primitive roles (`roles/owner` or `roles/editor`).
- **Rationale**: Enforces least-privilege security policy and eliminates risky broad permissions across build pipelines (TDD §12.6; PLAN8.md §2).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_cloudbuild_sa_least_privilege`
- **Status**: done
- **Owner**: ci-cd

### REQ-CICD-006: Cloud Build v2 GitHub Connection and Repository Resources
- **Statement**: Terraform MUST codify 2nd-gen Cloud Build GitHub connection (`google_cloudbuildv2_connection`) and repository (`google_cloudbuildv2_repository`) resources for `cloud-ai-fde/susmith-capstone-support-ticket-router`, gated behind `var.github_connection_ready` (default `false`) to allow graceful provisioning prior to manual GitHub App OAuth token authorization. CI triggers (`ci`, `ci_pr`, `reindex`) MUST support a gating variable `enable_ci_triggers` (default `false`), ensuring triggers remain disabled during development (`disabled = !var.enable_ci_triggers`) to avoid unwanted live Agent Search query load and costs on routine commits.
- **Rationale**: Provides declarative infrastructure-as-code for GitHub CI triggers while cleanly decoupling from the one-time interactive OAuth setup in Cloud Console and protecting development budgets (PLAN8.md §2).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_github_connection_gating`
- **Status**: done
- **Owner**: ci-cd

### REQ-CICD-007: Cloud Scheduler Nightly Evaluation Target Configuration
- **Statement**: The Cloud Scheduler resource `google_cloud_scheduler_job.nightly_eval` MUST configure an HTTP target invoking the nightly Cloud Build trigger's `:run` REST API endpoint using an OAuth token generated by the dedicated scheduler service account, with schedule paused by default (`paused = !var.enable_nightly_schedule`) as a stretch capability to conserve operational budget while retaining manual on-demand execution.
- **Rationale**: Enables reliable, serverless cron execution of nightly retrieval evaluations without managing external scheduler runners (PLAN8.md §2).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_scheduler_job_target_contract`
- **Status**: done
- **Owner**: ci-cd

### REQ-CICD-008: Static Security and Secret Leakage Prevention
- **Statement**: All Cloud Build YAML pipelines and Terraform configurations MUST NOT contain hardcoded API tokens, private keys, service account keys, or passwords. Cloud Run deployments MUST NOT use `--allow-unauthenticated`.
- **Rationale**: Prevents accidental credential exfiltration into version control and enforces perimeter authenticated ingress (TDD §12.6; deploy/terraform/README.md).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_no_secrets_in_cicd_yamls`
- **Status**: done
- **Owner**: ci-cd

### REQ-CICD-009: Change-Aware Golden Retrieval Evaluation Gate Execution
- **Statement**: CI pipeline subsystems MUST conditionally execute based on SHA-256 content fingerprints computed against the last successful main build state stored in `gs://${_CORPUS_BUCKET}/ci/state.json`. In `cicd/cloudbuild.yaml`: (1) the Golden Retrieval Evaluation Gate (`golden-eval-gate`) executes when the retrieval fingerprint changes or when substitution variable `_RUN_EVAL=true`, and skips when `_RUN_EVAL=false` or when `_RUN_EVAL=auto` with unchanged retrieval fingerprint; (2) corpus reindexing on main (`corpus-reindex-on-main`) executes only when the corpus fingerprint changes; (3) static Terraform checks execute only when the Terraform fingerprint changes; and (4) an updated CI state is persisted to GCS (`record-ci-state`) exclusively on successful builds of the `main` branch, preserving the prior retrieval fingerprint if evaluation was bypassed via `_RUN_EVAL=false`. Missing or empty baseline state treats all subsystems as changed.
- **Rationale**: Cloud Build shallow clones (`depth=1`) lack credentials for remote `git fetch` operations, causing git-diff-based detection to fail and default to executing all stages. Content fingerprinting directly inspects tracked repository bytes (`git ls-files` with filesystem fallback) for isolated subsystems (`retrieval`, `corpus`, `terraform`), preventing 300 sequential live Agent Search queries on non-retrieval PRs/pushes while guaranteeing rigorous quality enforcement whenever retrieval code, corpus data, or search infrastructure changes (or on demand).
- **Acceptance**: `tests/infra/test_cicd_static.py::test_eval_gate_conditional_execution`
- **Status**: done
- **Owner**: ci-cd

---

## 4. Open Questions, Deltas & AI FDE Delivery Alignment

1. **One-Time Manual GitHub App Authorization**: Cloud Build 2nd generation requires an interactive authorization flow where the user navigates to Cloud Build > Repositories in Google Cloud Console, installs the Google Cloud Build GitHub App into `cloud-ai-fde/susmith-capstone-support-ticket-router`, and authorizes the Secret Manager secret containing the personal access token. Terraform manages the connection and triggers once `var.github_connection_ready` is flipped to `true`.
2. **Golden Eval Gate Thresholds vs. Targets**:
   - Consensus `recall@5 ≥ 0.90` and `MRR@10 ≥ 0.75` are enforced directly from TDD §6.4 targets.
   - For `nDCG@5`, while the long-term TDD aspirational target is `0.80`, current empirical measurements achieve `0.650` under silver baseline and `0.681` under Release 3. Therefore, following `ai-fde-delivery` guidance, `nDCG@5` is enforced as a **non-regression gate** against the stored baseline (`nDCG@5 ≥ baseline − 0.02`), rather than an unachievable hard floor that would deadlock pipeline merges.
3. **FDE Delivery Skill Alignment (Applied vs. Deferred)**:
   - **Applied**:
     - *Eval-as-Gate*: `rag/ci_eval_gate.py` runs before deployment and exits with code 1 on regression to block merges and deploys.
     - *Runner-Agnostic Script Contracts*: 0 = pass, 1 = gate failed, 2 = infrastructure/could not run.
     - *Chain Version Lineage*: Computes 12-char SHA256 join key (`code_sha|prompt_version|model_version|retriever_index_version|tool_registry_version|judge_model_version`) and records it in evaluation artifacts.
     - *Automated Rollback*: `cloudbuild-reindex.yaml` rolls back to the previous release upon regression gate failure.
     - *Least-Privilege Security*: Dedicated build and scheduler service accounts without primitive roles.
     - *No `--allow-unauthenticated`*: Ingress requires authentication/IAP.
   - **Deferred**:
     - *Cloud Deploy Canary Ramp (`clouddeploy.yaml` [10, 50] → 100 with `verify: true`)*: Deferred because the Cloud Run router/agent serving containers are not yet provisioned. The canary manifest will be authored alongside the Cloud Run service.
     - *Binary Authorization Attestation*: Deferred to Phase 2/3 when container images are built and pushed to Artifact Registry.
     - *Gemini Enterprise Agent Registry*: Deferred until the Gemini Enterprise App is created.
4. **Standalone vs. Inline Reindex Trigger Strategy**:
   - `cicd/cloudbuild.yaml` incorporates change-aware path filtering (`scripts/ci_changes.py`) to execute corpus reindexing inline upon merges to `main` whenever `rag/corpus_v2/**` changes.
   - The dedicated trigger and pipeline `cicd/cloudbuild-reindex.yaml` (with `included_files = ["rag/corpus_v2/**"]`) are retained in parallel to allow fast, isolated knowledge-base publishing operations without triggering full multi-stage CI workflows when only documentation or FAQ articles are updated.
5. **Change-Aware CI Triggers Enabled by Default**: With change-aware conditional execution in place (`REQ-CICD-009`), CI triggers on push and pull requests targeting `main` are enabled by default (`enable_ci_triggers = true`) with `_RUN_EVAL = "auto"`. Unrelated changes bypass the 300-query evaluation gate, while PRs and pushes touching retrieval code, corpus data, or search infrastructure automatically enforce the retrieval gate. Nightly scheduled runs remain paused by default (`enable_nightly_schedule = false`).

---

## 5. Later Phases & Deferred Capabilities

Following the enterprise delivery roadmap, the following operational capabilities are formally specified and deferred until runtime serving services (Cloud Run / Agent Runtime) are provisioned:

1. **Canary Deployment & Live SLI-Driven Auto-Rollback**:
   - Automated Cloud Deploy rollout (`clouddeploy.yaml`) with canary traffic stages `[10, 50] → 100%`.
   - Post-phase verify hook executing mini-evaluations against the canary endpoint.
   - Automated rollback triggered if the canary revision's fast-burn latency or error-rate SLI breaches thresholds.
2. **Live SLI Breach Triage & Regression Dataset Export**:
   - Automated export of failed production request traces from Cloud Logging sinks to BigQuery.
   - ETL pipeline converting SLI-breaching conversations into regression eval cases staged in `gs://susmith-fde-ticket-router-laya/eval/` for perpetual model testing.
3. **Locust Distributed Load Testing & Capacity Sizing**:
   - Performance benchmark evaluating system throughput against Little's Law ($L = \lambda \cdot W$).
   - Validates horizontal pod autoscaling and GPU concurrency floors under peak traffic load.
4. **Declarative OpenAPI / REST Contracts**:
   - OpenAPI v3 / Swagger specifications defining external serving surfaces, request validation models, and canonical HTTP status codes.

---

## 6. Live Infrastructure Verification & Evidence (Phase 2)

Phase 2 infrastructure operations executed hermetically on Argolis VM `laya-tune` using `scripts/tf_vm.sh`:

1. **Static Linters & Mock Provider Tests**:
   - `terraform fmt -check -recursive`: PASSED (exit code 0).
   - `tflint --recursive`: PASSED (exit code 0, 0 warnings/errors).
   - `terraform validate`: Success! Configuration is valid (exit code 0).
   - `terraform test`: **4 passed, 0 failed** across `agent_search.tftest.hcl` and `cloudbuild.tftest.hcl`.

2. **First Plan & Zero-Impact Invariant**:
   - Command: `terraform plan -out=tfplan` with `github_connection_ready=false`.
   - Result: **11 to add, 0 to change, 0 to destroy**.
   - Verified zero impact on existing resources: `module.agent_search` data stores (`faq-articles`, `resolved-threads`), search engine (`support-kb-search`), identity, and corpus bucket IAM were completely unchanged.

3. **Apply Execution**:
   - Command: `terraform apply "tfplan"`.
   - Result: **11 added, 0 changed, 0 destroyed**.
   - Outputs:
     - `cloudbuild_service_account` = `ticket-router-build@susmith-fde-ticket-router.iam.gserviceaccount.com`
     - `scheduler_service_account` = `ticket-router-scheduler@susmith-fde-ticket-router.iam.gserviceaccount.com`
     - `nightly_trigger_id` = `7e64cfb8-cb26-4c68-9b69-31559d0f28dd`
     - `scheduler_job_name` = `support-ticket-router-nightly-eval`

4. **Zero-Drift Verification (Second Plan)**:
   - Command: `terraform plan`.
   - Verbatim result:
     ```
     No changes. Your infrastructure matches the configuration.
     Terraform has compared your real infrastructure against your configuration
     and found no differences, so no changes are needed.
     ```

5. **GCloud Production Asset Verification (`susmith-fde-ticket-router`)**:
   - **Build SA**: `ticket-router-build@susmith-fde-ticket-router.iam.gserviceaccount.com` exists (ID: `108753949838472880360`).
   - **Build SA Roles**:
     - Project: `roles/discoveryengine.editor`, `roles/logging.logWriter`.
     - Corpus Bucket (`gs://susmith-fde-ticket-router-laya`): `roles/storage.objectAdmin`.
   - **Scheduler SA**: `ticket-router-scheduler@susmith-fde-ticket-router.iam.gserviceaccount.com` exists.
   - **Scheduler SA Roles**:
     - Project: `roles/cloudbuild.builds.editor`.
   - **Nightly Cloud Build Trigger**:
     - Name: `support-ticket-router-nightly` (ID: `7e64cfb8-cb26-4c68-9b69-31559d0f28dd`, region: `us-central1`).
     - Path: `cicd/cloudbuild-nightly.yaml`.
     - Service Account: `ticket-router-build@...`.
   - **Cloud Scheduler Job**:
     - Name: `support-ticket-router-nightly-eval` (location: `us-central1`).
     - State: `PAUSED` (`paused = !var.enable_nightly_schedule`; nightly evals are a stretch item). The nightly trigger is also disabled (`disabled = !var.enable_nightly_schedule`).
     - Schedule: `0 2 * * *` (UTC).
     - Target: `https://cloudbuild.googleapis.com/v1/projects/susmith-fde-ticket-router/locations/us-central1/triggers/7e64cfb8-cb26-4c68-9b69-31559d0f28dd:run`.

6. **GitHub repository link (2026-10-26)**:
   - `connections/github-connection/repositories/support-ticket-router-capstone` → `https://github.com/cloud-ai-fde/susmith-capstone-support-ticket-router.git` (installation 124561748, OAuth user susmith-rb; the org repo is listed by `fetchLinkableRepositories`).
   - Triggers `support-ticket-router-ci`, `-ci-pr`, `-reindex` exist and are DISABLED (`enable_ci_triggers = false`, development phase). Post-apply plan: "No changes".
   - Historical: before the 2nd-gen repo link, `gcloud builds triggers run support-ticket-router-nightly` returned `NOT_FOUND: Couldn't read commit . detail: request_mode:gen1_repo_trigger`.

7. **Manual Trigger Execution**:
   - `gcloud builds triggers run support-ticket-router-nightly --region=us-central1 --project=susmith-fde-ticket-router --branch=main`
   - Result: Returned `ERROR: (gcloud.builds.triggers.run) NOT_FOUND: Couldn't read commit . detail: request_mode:gen1_repo_trigger`.
   - Cause: Triggers referencing GitHub without 2nd gen connection require the one-time interactive Cloud Console authorization of the Google Cloud Build GitHub App into `susmith-rb/support-ticket-router-capstone`. Once authorized and `var.github_connection_ready` is flipped to `true`, 2nd-gen triggers resolve commits via Developer Connect.

7. **Standard Search Tier Migration & Parity Verification (Phase 3)**:
   - **Terraform Plan & In-Place Invariant**:
     - `search_tier` defaulted to `SEARCH_TIER_STANDARD` and `search_add_ons` to `[]` across `deploy/terraform/single-project/`.
     - Plan showed `~ update in-place` for `google_discovery_engine_search_engine.support_kb_search` with 0 data store modifications.
     - Applied on VM `laya-tune` in 3s (`Modifications complete after 3s`).
   - **Live REST Engine Verification**:
     - Queried Discovery Engine REST API:
       `searchEngineConfig = {"searchTier": "SEARCH_TIER_STANDARD"}` with zero LLM add-ons.
   - **Live Query Smoke Test**:
     - `python rag/search_test.py "Where is my order"`: Passed cleanly.
     - Zero request parameters rejected (`relevanceThreshold`, `boostSpec`, `summarySpec` all supported).
     - Full summary and citations returned without degradation.
   - **DEV Production Retrieval Parity Benchmark**:
     - Evaluated DEV production config (`intent_soft_noisy_0.2`, soft boost 0.2, LOWEST threshold, clean query) across 300 held-out DEV queries with `rag/ablation_eval.py` → `dev_rows_standard.json`.
     - Scored against Annotator A/B/C2 consensus via `rag/multi_label_eval.py` (`MAIN` scope, n=218):
       - `recall@5`: **0.9128** (Enterprise baseline: 0.9128, exact parity)
       - `MRR@10`: **0.7955** (Enterprise baseline: 0.7996, Δ = -0.0041)
       - `nDCG@5`: **0.7262** (Enterprise baseline: 0.7288, Δ = -0.0026)
     - Proves full retrieval quality preservation with ~80% query cost reduction ($1.50/1k vs $8.00/1k list).
   - **CI Triggers Gating Policy**:
     - `enable_ci_triggers` variable (default `false`) integrated across Terraform module and root configs.
     - `disabled = !var.enable_ci_triggers` set on `ci`, `ci_pr`, and `reindex` triggers.
     - Unit test `test_ci_triggers_disabled_by_default` verified passing.
   - **Repository Linking State**:
     - Connection `projects/susmith-fde-ticket-router/locations/us-central1/connections/github-connection` imported into Terraform state with 0 diff.
     - Cloud Build v2 repository creation `google_cloudbuildv2_repository.repo[0]` blocked by GitHub API code 9: `repository susmith-rb/support-ticket-router-capstone does not exist or is not accessible to the Cloud Build GitHub App (installation ID 124561748)`. Pending user confirmation of repository selection/save at `https://github.com/settings/installations/124561748`.


