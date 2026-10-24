# Specification: Agent Search Infrastructure (`infra-agent-search`)

This specification defines the infrastructure-as-code architecture, execution invariants, security constraints, and data store provisioning for Google Cloud Agent Search (Discovery Engine) using Terraform.

---

## 1. Overview & Directory Layout

Following TDD §12.7 and Conductor Plan 2, all infrastructure is codified under `deploy/terraform/`:

```
deploy/terraform/
├── README.md
├── bootstrap/                    # Stage 0: Provisions GCS remote state bucket (local state)
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── versions.tf
│   └── .terraform.lock.hcl
└── single-project/               # Stage 1: Single-project infrastructure (GCS backend)
    ├── main.tf
    ├── variables.tf
    ├── outputs.tf
    ├── versions.tf
    ├── .terraform.lock.hcl
    ├── modules/
    │   ├── project-services/     # Enables core GCP APIs
    │   │   ├── main.tf
    │   │   ├── variables.tf
    │   │   ├── outputs.tf
    │   │   └── versions.tf
    │   └── agent-search/         # Discovery Engine in location "us"
    │       ├── main.tf
    │       ├── variables.tf
    │       ├── outputs.tf
    │       └── versions.tf
    └── tests/
        └── agent_search.tftest.hcl
```

### Hard Invariant: VM-Only Execution

**Terraform is NEVER installed or executed directly on the cloudtop workstation.**

1. **Network Egress Boundary**: Cloudtop workstations restrict direct outbound egress to public developer artifact mirrors (e.g. `releases.hashicorp.com`, `registry.terraform.io`). The Argolis Workbench VM `laya-tune` (zone `us-central1-b`) has Cloud NAT egress to download providers and binaries.
2. **Identity & Auth Boundary**: The capstone project enforces domain-restricted sharing and blocks Service Account key creation. The VM user operates under `susmith@gcp.altostrat.com` (Owner) with user Application Default Credentials (ADC) and an explicit quota project (`susmith-fde-ticket-router`), ensuring all provider calls carry `user_project_override = true`.
3. **Reproducibility & Wrapper**: Operations are driven via `scripts/tf_vm.sh` over an authenticated IAP SSH tunnel with pinned binaries (deploy/terraform/README.md lines 49–51):
   - Terraform `v1.9.8`
   - TFLint `v0.53.0`
   - Providers: `hashicorp/google` and `hashicorp/google-beta` `v6.50.0` locked in `.terraform.lock.hcl`.

---

## 2. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-INFRA-SEARCH-001` | Terraform Modular Directory Layout | `done` | `tests/infra/test_terraform_static.py::test_terraform_directories_exist` |
| `REQ-INFRA-SEARCH-002` | VM-Only Execution Invariant | `done` | `manual: ./scripts/tf_vm.sh validate single-project` |
| `REQ-INFRA-SEARCH-003` | Remote State Bucket Configuration | `done` | `tests/infra/test_terraform_static.py::test_backend_bucket_name` |
| `REQ-INFRA-SEARCH-004` | Regional and Search Resource Placement | `done` | `tests/infra/test_terraform_static.py::test_no_global_location_when_search_location_is_us` |
| `REQ-INFRA-SEARCH-005` | Discovery Engine Stores and Search Engine Contract | `done` | `tests/infra/test_terraform_static.py::test_agent_search_contract` |
| `REQ-INFRA-SEARCH-006` | Least-Privilege IAM and Security Policy | `done` | `tests/infra/test_terraform_static.py::test_tdd_12_6_security_policy_no_forbidden_iam` |
| `REQ-INFRA-SEARCH-007` | Read-Only Corpus Bucket Data Source | `done` | `tests/infra/test_terraform_static.py::test_corpus_bucket_data_source_not_managed` |
| `REQ-INFRA-SEARCH-008` | Agent Search Global to US Migration | `partial` | `manual: ./scripts/tf_vm.sh logged-run single-project single_project_plan plan` |
| `REQ-INFRA-SEARCH-009` | Discovery Engine Soft-Delete Lifecycle Awareness | `done` | `manual: gcloud compute ssh laya-tune --command "terraform state list"` |

---

## 3. Detailed Requirements

### REQ-INFRA-SEARCH-001: Terraform Modular Directory Layout
- **Statement**: Terraform code MUST be structured into `bootstrap/` (stage 0 remote state provisioning) and `single-project/` (stage 1 resource architecture), with reusable modules under `modules/project-services` and `modules/agent-search`. Every module MUST contain `main.tf`, `variables.tf`, and `outputs.tf`.
- **Rationale**: Enforces clean separation of concerns and clear module interface contracts (TDD §12.7; PLAN2.md §Workstream T).
- **Acceptance**: `tests/infra/test_terraform_static.py::test_terraform_directories_exist`
- **Status**: done
- **Owner**: infra-agent-search

### REQ-INFRA-SEARCH-002: VM-Only Execution Invariant
- **Statement**: All Terraform operations (init, fmt, lint, validate, plan, apply) MUST be executed exclusively on the Argolis Workbench VM `laya-tune` via the orchestration wrapper `scripts/tf_vm.sh` over an authenticated IAP SSH tunnel, NEVER on the local cloudtop.
- **Rationale**: Guarantees consistent network egress, pinned binary environments, and proper Argolis ADC quota-project credentials (TDD §12.7; deploy/terraform/README.md).
- **Acceptance**: `manual: ./scripts/tf_vm.sh validate single-project`
- **Status**: done
- **Owner**: infra-agent-search

### REQ-INFRA-SEARCH-003: Remote State Bucket Configuration
- **Statement**: Terraform state for `single-project` MUST be stored in GCS backend bucket `susmith-fde-ticket-router-tfstate` under prefix `terraform/state/single-project`, with location `us-central1`, object versioning enabled, uniform bucket-level access, and enforced public access prevention.
- **Rationale**: Prevents concurrent state corruption and enforces least-privilege state storage (TDD §12.7; deploy/terraform/README.md).
- **Acceptance**: `tests/infra/test_terraform_static.py::test_backend_bucket_name`
- **Status**: done
- **Owner**: infra-agent-search

### REQ-INFRA-SEARCH-004: Regional and Search Resource Placement
- **Statement**: All regional infrastructure (compute VMs, Cloud SQL, Cloud Run, GCS buckets) MUST be deployed in `us-central1`, while Agent Search data stores and search engines MUST be deployed in the `us` multi-region (host `us-discoveryengine.googleapis.com`).
- **Rationale**: Co-locates runtime compute and persistence for low-latency routing while placing Agent Search in a supported multi-region location (TDD §4.1; PLAN2.md).
- **Acceptance**: `tests/infra/test_terraform_static.py::test_no_global_location_when_search_location_is_us`
- **Status**: done
- **Owner**: infra-agent-search

### REQ-INFRA-SEARCH-005: Discovery Engine Stores and Search Engine Contract
- **Statement**: The Agent Search module MUST provision data store `faq-articles` with `content_config = "CONTENT_REQUIRED"`, data store `resolved-threads` with `content_config = "NO_CONTENT"`, and search engine `support-kb-search` linking both data stores with configurable tier and add-ons.
- **Rationale**: `faq-articles` contains unstructured text requiring document body parsing; `resolved-threads` contains structured JSONL records without document bodies, which Discovery Engine rejects if `content_config` is `CONTENT_REQUIRED` (TDD §6.2, §12.7; deploy/terraform/README.md lines 95–97).
- **Acceptance**: `tests/infra/test_terraform_static.py::test_agent_search_contract`
- **Status**: done
- **Owner**: infra-agent-search

### REQ-INFRA-SEARCH-006: Least-Privilege IAM and Security Policy
- **Statement**: Terraform configurations MUST NOT declare `google_project_iam_binding` resources, MUST NOT grant primitive roles (`roles/owner` or `roles/editor`), and MUST NOT generate service account keys.
- **Rationale**: Complies with Argolis security baselines and prevents non-authoritative role revocation (TDD §12.6; AGENTS.md; deploy/terraform/README.md).
- **Acceptance**: `tests/infra/test_terraform_static.py::test_tdd_12_6_security_policy_no_forbidden_iam`
- **Status**: done
- **Owner**: infra-agent-search

### REQ-INFRA-SEARCH-007: Read-Only Corpus Bucket Data Source
- **Statement**: The existing GCS bucket `susmith-fde-ticket-router-laya` MUST be imported as a read-only data source (`data "google_storage_bucket" "corpus_bucket"`) and MUST NOT be managed or destroyed by Terraform.
- **Rationale**: Protects raw customer support datasets, teacher labels, and fine-tuning checkpoints from accidental infrastructure lifecycle destruction (TDD §12.7; deploy/terraform/README.md).
- **Acceptance**: `tests/infra/test_terraform_static.py::test_corpus_bucket_data_source_not_managed`
- **Status**: done
- **Owner**: infra-agent-search

### REQ-INFRA-SEARCH-008: Agent Search Global to US Migration
- **Statement**: Migration of Agent Search from legacy `global` data stores to `us` multi-region stores SHOULD demonstrate retrieval quality parity on held-out queries before updating script default locations and decommissioning legacy resources.
- **Rationale**: Guarantees zero downtime and prevents search regression during cross-region migration (PLAN2.md §Phase 3).
- **Acceptance**: `manual: ./scripts/tf_vm.sh logged-run single-project single_project_plan plan`
- **Status**: partial
- **Owner**: infra-agent-search
- **Evidence**: `faq-articles` provisioned and loaded with 50 documents in `us` multi-region (recall@5 parity measured at 0.753 `us` vs 0.780 `global` in commit `a6f621c`). Search engine `support-kb-search` active in `us` linked to `faq-articles`. Data store `resolved-threads` definition updated to `content_config = "NO_CONTENT"` (matching structData schema); recreation in `us` and 81,103 thread document import pending clearance of Discovery Engine soft-delete tombstone.

### REQ-INFRA-SEARCH-009: Discovery Engine Soft-Delete Lifecycle Awareness
- **Statement**: Operations and disaster-recovery runbooks MUST account for Discovery Engine's asynchronous purge pipeline (`discovery-ds-deletion-pipeline`), which holds deleted data store IDs in soft-deleted state for hours to days and rejects immediate re-creation with HTTP 400 `FAILED_PRECONDITION`.
- **Rationale**: Prevents release pipeline deadlocks and provides documented teardown ordering (destroy search engine first to unblock data store deletion) (PLAN2.md §Workstream T; deploy/terraform/README.md).
- **Acceptance**: `manual: gcloud compute ssh laya-tune --command "terraform state list"`
- **Status**: done
- **Owner**: infra-agent-search
- **Evidence**: Empirically verified during `resolved-threads` data store recreation attempts (2026-10-23 and 2026-10-24). Discovery Engine rejected recreation with `Error 400: DataStore ... is being deleted, please wait for deletion to complete before recreating with the same ID. The deletion could take a couple of hours.` Root cause corroborated by internal Google issues b/570172520 and b/436210912: asynchronous Flume/Borg pipeline (`prod-us.discovery-ds-deletion-pipeline`) retains tombstones in Spanner. Confirmed operational rule: do not rename data store IDs on recreation; await soft-delete purge completion. Also verified that `support-kb-search` engine can be unlinked/recreated without affecting underlying data store `faq-articles` (50 docs intact).

---

## 4. Open Questions / Deltas

1. **Active Location Delta (Global vs. US)**: TDD §4.1 specifies that Agent Search data stores reside in `us` multi-region. Stage 1 Terraform has successfully provisioned `faq-articles` and `resolved-threads` in `us`, and `faq-articles` in `us` is loaded with 50 articles (measured recall@5 of 0.753 on `us` vs 0.780 on `global` in commit `a6f621c` parity evaluation; PLAN2.md §Phase 3). However, runtime scripts and evaluation tools still default to `global` via `--location` until final parity verification and index stabilization are complete.
2. **Omission of `google_discovery_engine_schema` Resource**: Terraform initially attempted to define schema resources. Discovery Engine automatically creates and updates `default_schema` upon ingestion for generic industry verticals; declaring static schema resources in Terraform triggered schema collision errors and was intentionally omitted (deploy/terraform/README.md lines 95–97).
3. **Lifecycle Ignore Changes for `document_processing_config`**: Discovery Engine injects default `digital_parsing_config` server-side upon creating `CONTENT_REQUIRED` data stores. Terraform plans detect this as drift and attempt to remove it, which forces resource replacement. A `lifecycle { ignore_changes = [document_processing_config] }` block is required on data store resources (deploy/terraform/README.md lines 98–99).
