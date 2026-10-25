# PLAN8: CI/CD with Cloud Build (mirrors the reference capstone's `cicd/` layout) (2026-10-25)

Reference: cloud-ai-fde/adisairam-capstone-onboarding-agent. The user shared its tree: there is NO
.github/workflows; CI/CD is `cicd/cloudbuild.yaml` (Cloud Build) + `cicd/clouddeploy.yaml` (Cloud
Deploy canary), with Terraform under `terraform/`. We mirror that pattern, scoped to what exists today.
Cloud Deploy is deferred because there's no deployable service yet; it gets added when the agent
service lands.

## Pipelines (cicd/)
1. `cicd/cloudbuild-ci.yaml`, triggered on push and on PRs to main (TDD §12.6 stages 1–2):
   - pip install -r requirements-dev.txt
   - pytest, including spec traceability
   - `publish_faq.py --lint-only` on the committed corpus manifest/docs, where available offline
   - terraform fmt -check / validate / tflint (hashicorp/terraform + tflint images)
   - no credentials needed beyond the build SA
2. `cicd/cloudbuild-reindex.yaml`, triggered on a push to main that touches the corpus paths
   (rag/corpus_v2/**) (TDD §6.5):
   - lint → publish_faq (next release) → retrieval regression gate on DEV (retrieval_eval / ablation
     with the production config)
   - Δrecall@5 ≥ −0.02 overall, otherwise roll back to the previous release
   - Needs the eval data in GCS (gs://…-laya/eval/), not the repo.
3. `cicd/cloudbuild-nightly.yaml`, SCHEDULED nightly through Cloud Scheduler → trigger run:
   - live retrieval health on DEV with the production config: recall@5 / MRR / nDCG@5 vs a stored
     baseline, plus p95 latency
   - writes the result JSON to gs://…-laya/eval/nightly/
   - fails the build if any metric drops more than 0.03 below the baseline, so a failed build is the
     alert

## Terraform (deploy/terraform/single-project/modules/cloudbuild)
- A dedicated build service account with least privilege:
  - discoveryengine.editor on the project (reindex)
  - storage objectAdmin on the -laya bucket
  - logging.logWriter
  - NO editor/owner (static test)
- `google_cloudbuildv2_connection` + `google_cloudbuildv2_repository` for GitHub
  `susmith-rb/support-ticket-router-capstone`. ONE-TIME MANUAL STEP: the user installs/authorizes the
  Cloud Build GitHub App on that repo, and the connection's OAuth token secret is created through the
  console flow. Document it and make the triggers depend on a variable `github_connection_ready`.
- `google_cloudbuild_trigger` ×3: ci (push + PR), reindex (push on main with an included_files
  filter), nightly (manual-invocation trigger).
- `google_cloud_scheduler_job` that calls the nightly trigger's :run endpoint daily, with an OAuth
  token from a scheduler SA.
- APIs: cloudbuild, cloudscheduler, secretmanager.
- Apply only from the VM through scripts/tf_vm.sh, with plan reviewed: no destroy of the existing
  agent-search resources.

## Tests and spec
- specs/ci-cd.md: new spec with REQ ids.
- tests/infra: static checks on cicd/*.yaml (required steps present, no secrets inlined, pytest step
  present, gate thresholds) and on the module (SA roles allow-list, schedule cron, included_files).
- tftest with a mock provider for the module.
