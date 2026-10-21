# AGENTS.md — rules for coding agents in this repo

Read this before changing code. The spec is [docs/TDD.md](docs/TDD.md); when code and spec
disagree, raise it and update the spec in the same change.

## Workflow

- **Spec first.** Every feature starts from the TDD section it implements (cite it in the commit
  message, e.g. `TDD §5.2`). Write or update the test before the implementation.
- **In the loop** (human reviews every diff): security, IAM, schemas, router thresholds, anything
  touching PII.
- **Outside the loop** (goal-driven): well-tested surfaces. Iterate lint → unit tests → eval until
  the stated goal passes, then report the final metrics.
- When you repeat a mistake or someone has to explain something twice, add it to
  [docs/LESSONS.md](docs/LESSONS.md) and, if it's a rule, here.

## Non-negotiables

1. **Rules only add escalations.** `final.escalate = rules OR tier1 OR tier2`. Never write code
   that clears an escalation set by an earlier tier (property test G2 guards this).
2. **Thresholds, model IDs and feature flags live in config**, never in code. Changing τ values
   needs a golden-set sweep artifact, not a code edit to make a test pass.
3. **No PII in logs, tests, fixtures or memory.** Log `ticket_id`, decisions and latencies, never
   message text. Fixtures use redaction tokens (`[ORDER]`, `[EMAIL]`, `@user`).
4. **Call Vertex endpoints serving Laya with `:rawPredict`.** `:predict` on shared endpoints sorts
   JSON keys and reorders choice options.
5. **Never retry a 429 synchronously.** The circuit breaker counts it immediately (TDD §10.4).
6. **Evals run through the Gen AI Evaluation Service wrapper**, not ad-hoc loops. `pytest` is for
   unit and integration tests.
7. **`customer_id` is never a tool parameter.** Tools read it from verified session claims (TDD §8.4,
   BOLA/IDOR).
8. **Don't commit data, checkpoints or secrets** (`data/`, `runs/`, `*.csv`, `kaggle.json`, `.env`).
9. Pin `google-adk` in deploy requirements to the version used locally.

## Environment facts (Argolis project `susmith-fde-ticket-router`)

- Org policies enforce: no external VM IPs (use Cloud NAT + IAP SSH), OS Login, Shielded VM with
  Secure Boot. There is no default VPC.
- SSH to the GPU VM as `admin@susmith.altostrat.com` via `--tunnel-through-iap`.
- Laya: train with `laya>=0.4.13` (`laya-train`), evaluate and serve with `laya==0.3.20` (the
  Model Garden container's version). Keep both working.
- Teacher model: `gemini-3.1-pro-preview` at location `global`.

## Commands

```bash
# training pipeline (on the VM, from training/router_s1)
bash pipeline/vm_setup.sh
(cd pipeline && ../.venv/bin/python test_redact.py)
RUN=v1 DATA=data/dataset/v1 BASE=english bash pipeline/train.sh
```
