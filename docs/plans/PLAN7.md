# PLAN7: train a reranker on generated samples to lift graded nDCG@5 (2026-10-25)

The user chose option 2 (model training). Target: TDD §6.4 graded nDCG@5 ≥ 0.80. Current: DEV 0.729 /
TEST 0.695, scored with C2 labels in MAIN scope on the release-5 production config. recall@5 and MRR
already meet their targets, and the gap is ranking order.

## Shared design
- Candidates at inference: the Agent Search top-10 for the production config (release 5, live), then
  the reranker reorders them, and the new top-5 is scored. Offline DEV evaluation uses the saved rows in
  `dev_rows_phaseD.json` (config intent_noisy_0.2). No new search calls are needed until TEST.
- Article text, the same for both arms: title + "Also asked as" questions + the first ~600 chars of the
  answer, taken from release-5 docs ($S/faq_v2p/docs).
- Training data, built once by worker B. Leakage rule: no thread in DEV or TEST, and no query text
  matching DEV/TEST.
  1. GRADED: 447 agent-labeled tweets (rag/corpus_v2/agent_decisions/disambiguation_pool_labels.jsonl),
     relevance primary=2, other relevant=1, everything else 0.
  2. SILVER: up to 3,000 customer first turns from themes_v2 threads outside DEV/TEST (English,
     support-like), relevance theme article=2, other=0. Balanced per article (cap about 80).
  3. Hard negatives per query: same-family siblings + the most-confused pairs from the DEV confusion
     map + random others. With 50 docs, every article can be a candidate.
  4. Validation for early stopping: 10% of TRAIN, by thread. DEV is used only for arm/config selection;
     TEST is run once, audited.
- Output: $S/rerank/train.jsonl, val.jsonl, docs.json, and a data card (counts, leakage proof).

## Arm A: cross-encoder (worker R1)
- Fine-tune an open cross-encoder (start `cross-encoder/ms-marco-MiniLM-L-6-v2`; try
  `BAAI/bge-reranker-base` if time allows) on the laya-tune VM, ON CPU, because the GPU is reserved for
  arm B. Graded targets use a regression or listwise loss.
- Rerank the top-10 and blend: final = α·rerank + (1−α)·search-rank prior, with α tuned on DEV.

## Arm B: Laya typed-decision choice (worker R2)
- A choice question with ≤5 options (the Laya choice limit) over article titles, a soft gold
  distribution from the graded relevance, `laya-train --loss soft-ce --shuffle-options`. Start from the
  router-s1 v2 or `english` base. Reuse training/router_s1's VM venvs (.venv-train 0.4.13; serve/eval
  0.3.20).
- Inference reranks the top-5 by probability. Laya can only see 5 options, so it reorders within the
  top-5.

## Decision
- Score each arm on DEV with rag/multi_label_eval.py (C2, MAIN). Report nDCG@5, recall@5, MRR, the
  lower bound, and added latency (CPU and GPU).
- The winner is the best DEV nDCG@5 with recall@5 ≥ 0.90 and MRR ≥ 0.75. If the winner gains ≥ +0.03,
  one audited TEST run follows: live search top-10 → rerank.
- Serving (Cloud Run / Vertex endpoint) and Terraform are designed only if TEST passes.
- Rules: spec-driven (add a REQ), pytest for data building and rerank logic, Terraform/VM only through
  the documented scripts, workers never commit, and the VM is stopped when idle.
