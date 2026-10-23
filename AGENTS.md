# AGENTS.md — rules for coding agents in this repo

## Spec-driven development

**Mandatory reading: [specs/README.md](specs/README.md).**

This repository strictly enforces Spec-Driven Development (SDD). Specifications live directly in the code under `specs/`.
- Every change starts by reading and editing the relevant spec in `specs/`, then updating tests, then the code. Update the spec in the SAME commit.
- Every requirement has a unique ID (e.g. `REQ-RAG-CORPUS-001`).
- Tests must reference requirement IDs in comments (`# REQ-...`).
- Every `done` requirement must be covered by at least one test in `tests/**` or an acceptance command explicitly marked `manual:`. Traceability is enforced offline by `tests/test_spec_traceability.py`.
- The design doc is [docs/TDD.md](docs/TDD.md) (the "why"). When code and TDD disagree, record the discrepancy under "Open questions / deltas" in the relevant spec file rather than resolving it unilaterally.

## Workflow

- **Spec first.** Every feature starts from the spec requirement and the TDD section it implements (cite both in the commit message, e.g. `REQ-RAG-CORPUS-001 / TDD §5.2`). Write or update the test before the implementation.
- **In the loop** (human reviews every diff): security, IAM, schemas, router thresholds, anything touching PII.
- **Outside the loop** (goal-driven): well-tested surfaces. Iterate lint → unit tests → eval until the stated goal passes, then report the final metrics.
- When you repeat a mistake or someone has to explain something twice, add it to [docs/LESSONS.md](docs/LESSONS.md) and, if it's a rule, here.

## Non-negotiables

1. **Rules only add escalations.** `final.escalate = rules OR tier1 OR tier2`. Never write code that clears an escalation set by an earlier tier (property test G2 guards this).
2. **Thresholds, model IDs and feature flags live in config**, never in code. Changing τ values needs a golden-set sweep artifact, not a code edit to make a test pass.
3. **No PII in logs, tests, fixtures or memory.** Log `ticket_id`, decisions and latencies, never message text. Fixtures use redaction tokens (`[ORDER]`, `[EMAIL]`, `@user`).
4. **Call Vertex endpoints serving Laya with `:rawPredict`.** `:predict` on shared endpoints sorts JSON keys and reorders choice options.
5. **Never retry a 429 synchronously.** The circuit breaker counts it immediately (TDD §10.4).
6. **Evals run through the Gen AI Evaluation Service wrapper**, not ad-hoc loops. `pytest` is for unit and integration tests.
7. **`customer_id` is never a tool parameter.** Tools read it from verified session claims (TDD §8.4, BOLA/IDOR).
8. **Don't commit data, checkpoints or secrets** (`data/`, `runs/`, `*.csv`, `kaggle.json`, `.env`).
9. Pin `google-adk` in deploy requirements to the version used locally.
10. **Terraform runs ONLY on the VM via `scripts/tf_vm.sh`.** Never install or execute Terraform directly on the cloudtop. Code is authored on the cloudtop and executed on Workbench VM `laya-tune` over IAP SSH.
11. **Offline LLM work runs on Jetski agents.** All offline article synthesis, theme regrouping, question generation, and LLM judging must run on Jetski agent models (Gemini 3.8 Flash high thinking), NEVER as Vertex AI calls billed to the Argolis project. Argolis quota is reserved strictly for runtime Agent Search queries and live serving.

## Environment facts (Argolis project `susmith-fde-ticket-router`)

- Org policies enforce: no external VM IPs (use Cloud NAT + IAP SSH), OS Login, Shielded VM with Secure Boot. There is no default VPC.
- SSH to the GPU VM `laya-tune` as `susmith@gcp.altostrat.com` via `--tunnel-through-iap`.
- GitHub push path runs on the VM using `sudo -u admin_susmith_altostrat_com` (where the repo clone and deploy key live; see PLAN2.md and deploy/terraform/README.md).
- Laya: train with `laya>=0.4.13` (`laya-train`), evaluate and serve with `laya==0.3.20` (the Model Garden container's version). Keep both working.
- Teacher model: `gemini-3.1-pro-preview` at location `global`.

## Commands

```bash
# training pipeline (on the VM, from training/router_s1)
bash pipeline/vm_setup.sh
(cd pipeline && ../.venv/bin/python test_redact.py)
RUN=v1 DATA=data/dataset/v1 BASE=english bash pipeline/train.sh

# terraform operations (driven from cloudtop via VM wrapper)
./scripts/tf_vm.sh sync
./scripts/tf_vm.sh validate single-project
./scripts/tf_vm.sh logged-run single-project single_project_plan plan
```
