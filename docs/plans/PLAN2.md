# Conductor plan 2: Terraform for Agent Search + move to `us` (2026-10-23)

## User decisions (direct)
- Terraform runs on an Argolis VM, NEVER on the cloudtop. The VM is `laya-tune` (Workbench, zone
  us-central1-b, project susmith-fde-ticket-router).
  - SSH: `gcloud compute ssh laya-tune --zone us-central1-b --project susmith-fde-ticket-router --account susmith@gcp.altostrat.com --tunnel-through-iap`
  - Prefix: `CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false`
  - You log in as Linux user `ext_susmith_gcp_altostrat_com` and have sudo. Start the VM with
    `gcloud workbench instances start laya-tune --location us-central1-b --project susmith-fde-ticket-router`.
    The first SSH can fail with exit 255 while it boots; wait about 60 s and retry. STOP it when done with
    `gcloud workbench instances stop ...`. It has a T4 GPU, so don't leave it running.
  - The VM has internet egress, so it can download Terraform from releases.hashicorp.com.
- Region: "build in us-central for now". Agent Search only supports global / us / eu, so:
  - Agent Search data stores and engine → `us` multi-region (API host `us-discoveryengine.googleapis.com`).
  - Everything regional → `us-central1`.
- Code is written in Jetski (cloudtop) in the repo; it reaches the VM via GCS (bundle/tar) and SSH.

## Repo
/usr/local/google/home/susmith/src/support-ticket-router-capstone
- tests: `.venv/bin/python -m pytest` (90 passing now; must stay green)
- Read: AGENTS.md; docs/TDD.md §4 (lines ~145-155), §6.2-6.6, §12.6-12.7 (Terraform modules, CI stages), §13
- Current live resources (all in `global`, created by rag/agent_search_setup.sh):
  - data stores `faq-articles` (v2 corpus, release 3) and `resolved-threads` (81,103 docs)
  - engine `support-kb-search` (Enterprise tier + LLM add-on)
- Corpus files:
  - `gs://susmith-fde-ticket-router-laya/corpus/faq-articles/v3/` (docs + manifest + import.jsonl)
  - `gs://susmith-fde-ticket-router-laya/corpus/resolved-threads/v1/`
- Scratch (never commit): /usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/

## Hard rules (both workers)
- No `git commit` / `git add` / `git checkout` / `git restore` / `git stash`. The conductor commits.
- Edit only your ASSIGNED_FILES_EXCLUSIVE.
- Unit tests are offline.
- Do NOT delete or modify the existing `global` resources.
- Return header: STATUS / MODIFIED_FILES / VERIFY_RECEIPT, using VERBATIM command output. The
  conductor re-runs every receipt, and a pasted or paraphrased receipt counts as a failure.
- Hooks block `python3 -c`, `grep` and `find`; write script files instead.

## Workstream T (Terraform) – worker T
Layout, per TDD §12.7, scoped to what exists now:
- deploy/terraform/bootstrap/ creates the state bucket `susmith-fde-ticket-router-tfstate` (us-central1,
  versioning on, uniform access, public-access prevention), using local state.
- deploy/terraform/single-project/ uses a GCS backend in that bucket; variables project_id, region
  (default us-central1), search_location (default "us"), search_tier, search_add_ons, labels
  (cost_center). It contains:
  - modules/project-services: discoveryengine, aiplatform, storage, iam, serviceusage, cloudresourcemanager
  - modules/agent-search:
    - `google_discovery_engine_data_store` faq-articles + resolved-threads: content_config CONTENT_REQUIRED,
      industry_vertical GENERIC, solution_types SOLUTION_TYPE_SEARCH, location var.search_location
    - `google_discovery_engine_search_engine` support-kb-search linking both stores, with
      search_engine_config tier/add-ons from variables (defaults matching today: Enterprise + LLM, so
      evals stay comparable)
    - optional `google_discovery_engine_schema` for faq structData fields (article_id, version, status,
      effective_from, effective_to, intent_family, policy_version) if the provider supports it; otherwise
      document why not
  - IAM: the Discovery Engine service agent gets objectViewer on the corpus bucket (it exists after the
    first API call; use `google_project_service_identity` if available)
  - existing bucket `susmith-fde-ticket-router-laya`: reference it as a data source; do NOT manage or
    destroy it
- Pin the provider (google >= current), pin terraform required_version, and commit `.terraform.lock.hcl`.
- Tests:
  - `terraform test` files (*.tftest.hcl) with `mock_provider "google"` that assert data store
    locations = var.search_location, both stores are linked to the engine, the tier variable flows
    through, and the labels are set.
  - a pytest `tests/infra/test_terraform_static.py` (offline) that parses the .tf files as text. It
    asserts: no resource named in "global" when search_location="us"; the backend bucket name; that
    no `google_project_iam_binding` / `roles/editor` / `roles/owner` is used (TDD §12.6 policy); and
    that every module has variables.tf/outputs.tf.
- On the VM:
  1. install terraform (pin a version, sha256-verify) and tflint into ~/bin
  2. pull the repo content (upload a tar of deploy/terraform to `gs://…-laya/tf-src/` from the cloudtop)
  3. `terraform fmt -check -recursive`, `tflint`, `terraform init`, `terraform validate`, `terraform test`
  4. bootstrap apply
  5. `plan` (save the plan output to GCS and to scratch)
  6. `apply`, which creates the `us` data stores and engine
  Report `terraform state list` and the apply summary. Copy all logs to scratch/tf/.
- scripts/tf_vm.sh: the reproducible wrapper (upload tar → ssh → run the given terraform subcommand →
  copy the log back). Document it in deploy/terraform/README.md, including why it runs on the VM.

ASSIGNED_FILES_EXCLUSIVE (T): deploy/terraform/**, tests/infra/**, scripts/tf_vm.sh, .gitignore (add .terraform/ and *.tfstate*, but keep the lock files)

## Workstream L (location parametrization) – worker L
- Add `--location` (default "global" for now; the conductor flips it after parity is verified) to:
  - rag/publish_faq.py
  - rag/retrieval_eval.py
  - rag/clean_eval.py
  - rag/search_test.py
  - rag/evalsvc_groundedness.py (only if it touches discoveryengine)
- Add one shared helper `rag/de_endpoint.py`: `api_base(location)` returns
  `https://discoveryengine.googleapis.com/v1/` for global and
  `https://{loc}-discoveryengine.googleapis.com/v1/` otherwise; plus resource-name builders for data store
  and serving config. All scripts use it.
- rag/agent_search_setup.sh: add a header saying it is superseded by deploy/terraform (keep it for history).
- Tests: tests/rag/test_de_endpoint.py; extend the existing tests so each script builds the `us` URL/resource
  names when `--location us` is passed (mock urlopen and assert on the request URL).
- Keep the default behavior identical.

ASSIGNED_FILES_EXCLUSIVE (L): rag/de_endpoint.py, rag/publish_faq.py, rag/retrieval_eval.py, rag/clean_eval.py, rag/search_test.py, rag/evalsvc_groundedness.py, rag/agent_search_setup.sh, tests/rag/test_de_endpoint.py, tests/rag/test_publish_faq.py, tests/rag/test_retrieval_eval.py, tests/rag/test_clean_eval.py

## Phase 3 (conductor, after T and L)
1. Import the corpora into the `us` stores: publish_faq `--location us` for faq-articles, and a
   documents:import from the GCS jsonl for resolved-threads.
2. Tier-1 parity eval on `us` against the global numbers: recall@5 on the gate set 0.780, ±noise.
3. Flip the defaults to `us`.
4. Ask the user before deleting the global resources.
5. Update TDD §4 with the location note, commit, and push.
