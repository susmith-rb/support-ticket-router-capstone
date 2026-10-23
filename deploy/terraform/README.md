# Capstone Terraform Infrastructure

This directory contains the Terraform configuration for the Support Ticket Router & Assistant capstone project on Google Cloud Platform.

## Architecture & Directory Layout

The Terraform layout follows TDD §12.7 and Conductor Plan 2:

```
deploy/terraform/
├── README.md
├── bootstrap/               # Stage 0: Provisions GCS remote state bucket (local state)
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── versions.tf
│   └── .terraform.lock.hcl
└── single-project/          # Stage 1: Single-project infrastructure (GCS backend)
    ├── main.tf
    ├── variables.tf
    ├── outputs.tf
    ├── versions.tf
    ├── .terraform.lock.hcl
    ├── modules/
    │   ├── project-services/ # Enables core GCP APIs
    │   │   ├── main.tf
    │   │   ├── variables.tf
    │   │   ├── outputs.tf
    │   │   └── versions.tf
    │   └── agent-search/     # Agent Search (Discovery Engine) in "us" multi-region
    │       ├── main.tf
    │       ├── variables.tf
    │       ├── outputs.tf
    │       └── versions.tf
    └── tests/
        └── agent_search.tftest.hcl  # Terraform test suite (mock provider)
```

## Hard Invariant: VM-Based Execution

**Terraform is NEVER installed or executed directly on the cloudtop.**

All Terraform operations, provider downloads, validations, and applies run on the dedicated Argolis Workbench VM `laya-tune` (zone `us-central1-b`, project `susmith-fde-ticket-router`) over an authenticated IAP SSH tunnel as `susmith@gcp.altostrat.com`.

### Why Terraform Runs on the VM
1. **Network Egress & Registry Access**: Cloudtop workstations restrict direct outbound egress to public developer artifact mirrors (e.g. `releases.hashicorp.com`, `registry.terraform.io`). The Workbench VM has internet egress to download providers and binaries.
2. **Identity & Auth Boundary**: The capstone project enforces domain-restricted sharing and blocks Service Account key creation. The VM user operates under `susmith@gcp.altostrat.com` (Owner) with user Application Default Credentials (ADC) and an explicit quota project (`susmith-fde-ticket-router`), ensuring all provider calls carry `user_project_override = true`.
3. **Reproducibility**: Binaries on the VM are pinned and checksum-verified:
   - Terraform `v1.9.8` (`186e0145f5e5f2eb97cbd785bc78f21bae4ef15119349f6ad4fa535b83b10df8`)
   - TFLint `v0.53.0` (`bb0a3a6043ea1bcd221fc95d49bac831bb511eb31946ca6a4050983e9e584578`)
   - Providers: `hashicorp/google` `v6.50.0` and `hashicorp/google-beta` `v6.50.0` locked in `.terraform.lock.hcl`.

## Automation Wrapper: `scripts/tf_vm.sh`

The helper script `scripts/tf_vm.sh` automates packaging the local code, uploading it to GCS (`gs://susmith-fde-ticket-router-laya/tf-src/terraform.tar.gz`), extracting it on `laya-tune`, executing Terraform over SSH, and retrieving receipts and lock files.

### Exact Usage

```bash
# 1. Sync local deploy/terraform/ changes to VM
./scripts/tf_vm.sh sync

# 2. Run static checks and linters on the VM
./scripts/tf_vm.sh fmt-check
./scripts/tf_vm.sh tflint

# 3. Validate configurations
./scripts/tf_vm.sh validate bootstrap
./scripts/tf_vm.sh validate single-project

# 4. Run Terraform unit tests (mock provider)
./scripts/tf_vm.sh test single-project

# 5. Execute Bootstrap (creates state bucket susmith-fde-ticket-router-tfstate)
./scripts/tf_vm.sh run bootstrap init
./scripts/tf_vm.sh logged-run bootstrap bootstrap_apply apply -auto-approve

# 6. Execute Single-Project Plan & Apply
./scripts/tf_vm.sh run single-project init
./scripts/tf_vm.sh logged-run single-project single_project_plan plan -out=tfplan
./scripts/tf_vm.sh logged-run single-project single_project_apply apply tfplan

# 7. Verify zero-diff on re-plan
./scripts/tf_vm.sh logged-run single-project single_project_plan_second plan

# 8. Pull lock files back into version control
./scripts/tf_vm.sh pull-locks
```

## Discovery Engine Details & Provider Field Notes

### Location: `us` Multi-Region
The data stores (`faq-articles`, `resolved-threads`) and engine (`support-kb-search`) are created in location `us` (API endpoint `https://us-discoveryengine.googleapis.com/v1/`). The legacy `global` data stores remain untouched for migration parity.

### Discovery Engine Schema Handling
In Discovery Engine, data stores configured with `content_config = "CONTENT_REQUIRED"` and `industry_vertical = "GENERIC"` automatically generate and update `default_schema` upon document ingestion (`documents:import`). Explicitly declaring a static `google_discovery_engine_schema` resource prior to document import causes schema collision or import failures; schema evolution is therefore driven by the structured metadata in the corpus import payload (`import.jsonl`).

### `document_processing_config` Lifecycle Rule
Upon creation of `CONTENT_REQUIRED` data stores, Discovery Engine initializes an immutable server-side `document_processing_config` with `digital_parsing_config`. If omitted in Terraform configuration, Terraform attempts to destroy this block on subsequent plans, which triggers `# forces replacement`. A `lifecycle { ignore_changes = [document_processing_config] }` block is set on each data store resource to preserve clean drift detection.
