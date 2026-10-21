# Ticket Router: Laya Tier-1 fine-tuning pipeline

Reproducible pipeline that turns the Kaggle **Customer Support on Twitter** corpus into a fine-tuned
[Laya](https://github.com/NandhaKishorM/laya) typed-decision router (Tier 1 in the TDD's
System-1 / System-2 design). Every step is a script; re-running with the same inputs and seeds
reproduces the dataset and the run manifest.

```
twcs.csv ──prepare_twcs.py──▶ AmazonHelp first-turn messages + threads (PII-redacted)
        ──label_teacher.py──▶ Gemini teacher labels, 3-sample self-consistency (soft vote shares)
        ──build_dataset.py──▶ laya-train JSONL: train / val / test (stratified)
        ──train.sh──────────▶ laya-train fine-tune (T4/L4) ─▶ evaluate.py (serving version) ─▶ runs/<RUN>/
```

## Environment

| Item | Value |
| --- | --- |
| GCP project | `susmith-fde-ticket-router` (Argolis org `susmith.altostrat.com`) |
| Bucket | `gs://susmith-fde-ticket-router-laya` — `data/raw`, `data/processed`, `data/labels`, `data/dataset/<v>`, `runs/<RUN>`, `pipeline/` (code mirror) |
| GPU VM | Workbench `laya-tune`, `us-central1-b`, n1-standard-8 + 1×T4, private IP, Cloud NAT egress |
| SSH | `gcloud compute ssh laya-tune --zone us-central1-b --project susmith-fde-ticket-router --account admin@susmith.altostrat.com --tunnel-through-iap` (OS Login is org-enforced; the in-org admin identity works immediately, the `gcp.altostrat.com` identity needs `roles/compute.osLoginExternalUser` at org level) |
| Teacher model | `gemini-3.1-pro-preview` on Vertex AI, location `global` (GA id `gemini-3.1-pro` returned 404 in this project on 2026-10-21; ~11 s per call with thinking=low). Measured ≈ 547 in / 42 out / 345 thinking tokens per call ≈ \$0.0057 per call, ≈ \$0.017 per message at 3 samples (`pipeline/usage_probe.py`) |
| Laya versions | `.venv` = `laya[serve]==0.3.20` (what the Model Garden serving container pins; used for eval) · `.venv-train` = `laya==0.4.13` (only 0.4.x has `laya-train`) |

### Org-policy gotchas hit when creating the VM (Argolis defaults)

- No default VPC → `infra`: custom VPC `capstone-vpc`, subnet `capstone-us-central1` (10.10.0.0/20, Private Google Access).
- `compute.vmExternalIpAccess` = deny → use `--disable-public-ip` + Cloud Router/NAT (`capstone-router`/`capstone-nat`) for GitHub/HF egress; SSH through IAP (firewall `capstone-allow-iap-ssh`, 35.235.240.0/20).
- `compute.requireShieldedVm` → `--shielded-secure-boot=true --shielded-vtpm=true --shielded-integrity-monitoring=true`.
- T4 stockouts happen per zone (us-central1-a was out on 2026-10-21; -b worked). No L4 quota by default — request it before deploying the `router-s1` endpoint.
- The VM's default compute SA needs `objectAdmin` on the bucket and `roles/aiplatform.user` for the teacher calls.

## Run it

All commands run **on the VM** from this directory. `training/router_s1/` is mirrored to
`gs://susmith-fde-ticket-router-laya/pipeline` and synced to `~/ticket-router-laya` on the VM:

```bash
# on the cloudtop/laptop, from the repo root
gsutil -m rsync -r -x '\.git/.*' training/router_s1 gs://susmith-fde-ticket-router-laya/pipeline
# on the VM
gsutil -m rsync -r gs://susmith-fde-ticket-router-laya/pipeline ~/ticket-router-laya
cd ~/ticket-router-laya && export BUCKET=gs://susmith-fde-ticket-router-laya PROJECT=susmith-fde-ticket-router
bash pipeline/vm_setup.sh                                   # once
```

1. **Prepare** (CPU, ~5 min for 2.8M rows)
   ```bash
   mkdir -p data/raw && gsutil cp "$BUCKET/data/raw/archive (1).zip" data/raw/archive.zip && (cd data/raw && unzip -o archive.zip)
   .venv/bin/python pipeline/prepare_twcs.py --csv "$(find data/raw -name twcs.csv)" --brand AmazonHelp --out data/processed/
   gsutil -m cp data/processed/* "$BUCKET/data/processed/"
   ```
2. **Teacher labels** (API-bound; resumable — re-running skips labeled threads)
   ```bash
   cd pipeline && ../.venv/bin/python label_teacher.py --inp ../data/processed/inbound_first_turn.parquet \
       --out ../data/labels/labels.jsonl --n 2000 --samples 3 --model gemini-3.1-pro-preview --location global --concurrency 32 && cd ..
   gsutil cp data/labels/labels.jsonl "$BUCKET/data/labels/"
   ```
3. **Build dataset** (seconds)
   ```bash
   (cd pipeline && ../.venv/bin/python build_dataset.py --labels ../data/labels/labels.jsonl --out ../data/dataset/v1)
   gsutil -m rsync -r data/dataset/v1 "$BUCKET/data/dataset/v1"
   ```
4. **Train + evaluate**
   ```bash
   RUN=v1 DATA=data/dataset/v1 BASE=english bash pipeline/train.sh
   ```
   Writes `runs/v1/{ckpt/, dry_run.txt, train.log, eval_test.json, manifest.json}` and the zero-shot
   baseline `runs/baseline_english_v1.json`, then syncs to `$BUCKET/runs/`.

Knobs: `LOSS=rlcd|soft-ce` (default soft-ce, best on the vendor's T4 benchmark), `BASE=english|typed-decisions`,
`TARGET_UPDATES` (default 300 optimizer updates; epochs derived from it, clamped 3–12), `SEED`.

## What the evaluation reports (`evaluate.py`)

- `sentiment`, `intent`: accuracy vs teacher argmax, macro-F1, ECE (15-bin).
- `urgency`: exact and within-one accuracy.
- `escalation`: `p_esc = max(P(escalate), P(angry))`, swept τ 0.05–0.95 → recall, false-escalation rate,
  escalated share; `tau_esc_max_recall1` = the largest τ that keeps recall 1.0 (the TDD's τ_esc rule).
- `latency_ms`: in-process p50/p95 on the VM GPU (not endpoint latency).

> [!IMPORTANT]
> `test.jsonl` is **teacher-labeled**. It is not the human-verified golden set. To promote it,
> have two people review the cases, record agreement (Cohen's κ ≥ 0.8), fix labels, and save as
> `data/golden/v1.jsonl`; τ_esc for production must be set on the golden set.

## Serving the checkpoint

The `runs/<RUN>/ckpt/` directory has the layout the Model Garden container loads from `artifactUri`
(`model.safetensors`, `rl_agent_config.json`, `encoder/`, `tokenizer/`):

```bash
gsutil -m rsync -r runs/v1/ckpt gs://susmith-fde-ticket-router-laya/checkpoints/router-s1/v1
# then upload a Vertex Model with the laya-serve image and artifactUri=that path; call with :rawPredict
```

## Results log

| Run | Data | Base | Loss | Epochs | sentiment acc / F1 | intent acc / F1 | τ_esc @ recall 1.0 (false-esc rate) | p50 ms | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| smoke-cs-v0 | public typed-decisions `customer_service` (300 train / 100 test = 500 decisions) | english | soft-ce + shuffle | 8 | overall decision accuracy **0.376 → 0.778** (Brier 0.306 → 0.064; ECE 0.108 → 0.128) | — | — | 41–46 (T4, in-process, warm) | Pipeline check, not our task. 1,627 s on T4 (long multi-turn states). Checkpoint loads and answers our 4 router questions under laya 0.3.20 (serving version) |
| baseline (zero-shot) | v1 test (199 tweets) | `convaiinnovations/laya` (english) | — | 0 | 0.583 / 0.514 | 0.633 / 0.555 | 0.19 (FER 0.98) | 39 | urgency exact 0.19, within-one 0.55; intent ECE 0.17 |
| baseline (zero-shot) | v1 test | `typed-decisions` | — | 0 | 0.608 / 0.546 | 0.658 / 0.556 | 0.29 (FER 0.97) | 39 | urgency exact 0.17; intent ECE 0.24 |
| **v1** | v1: 2,000 AmazonHelp tweets, Gemini 3.1 Pro preview ×3 soft labels; 1,602 / 199 / 199 | english | soft-ce + shuffle | 4 (376 updates) | **0.754 / 0.754** (ECE 0.078) | **0.814 / 0.750** (ECE 0.053) | 0.02 (FER 0.92); at 0.15: recall 0.935, FER 0.24 | **39 (p95 46)** | urgency exact 0.69, **within-one 0.98**. 1,269 s on T4. Tier-1 alone can't reach recall 1.0: of the 5 positives below 0.15, 3 are non-English (fr/es), 1 is a security/privacy report, 2 have split (2/3) teacher votes. ckpt sha `5a4fed642691dc86` |
