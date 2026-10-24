# PLAN4: TDD-conformant retrieval measurement (pre-registered 2026-10-24, before any label is seen)

Goal: measure retrieval the way TDD §6.4 defines `retrieval/v1`: 1–3 relevant articles per query from
two annotators, graded nDCG. Then decide whether the system itself still needs improvement.

## Labels
- Two independent BLIND annotators (A, B). Both are Jetski Gemini Flash agents, which is allowed under the
  offline-LLM billing rule. They see only the query text and the article catalog (release 5 docs): no
  search results, no silver gold, and not each other's labels. Guide: scratch rag/labeling/GUIDE.md.
  Inputs: 600 queries (TEST + DEV, shuffled).
- Adjudicator C, also blind to system output, sees each query, the catalog, and A's and B's labels:
  - It resolves disagreements on `support_request`, `language` and `primary`.
  - For every query it picks the final relevant set: 1–3 docs drawn from A ∪ B, plus at most one doc
    neither chose, with a note explaining why.
- Agreement is reported: Cohen's κ on support_request; exact-match rate and κ on primary; and the Jaccard
  overlap of the relevant sets.
- Honesty note: these are LLM annotators. The TDD asks for two annotators, and human confirmation of a
  sample (the user and a second labeler) remains pending. The report must say so.

## Scope (fixed now)
- MAIN scope = queries where the consensus says support_request = true AND language = "en" AND
  primary ≠ null. Multilingual is out of scope per the user; non-support messages are routed away by the
  router and never reach FAQ retrieval.
- Also report the ALL-support scope (support_request = true, any language, primary ≠ null) and the counts
  of excluded queries by reason.

## Metrics (fixed now)
- recall@5: any consensus-relevant doc in the top 5.
- MRR@10: rank of the first consensus-relevant doc.
- nDCG@5 graded: primary = 2, other relevant = 1. IDCG comes from the query's own label set.
- Sensitivity bounds, reported alongside but NOT used as the gate:
  - LOWER = only the agreed primary is relevant
  - UPPER = A ∪ B are all relevant
- Single-silver-gold metrics are kept for continuity.

## System outputs
- TEST: one new audited run of the production config (LOWEST + clean + soft intent boost 0.2 with
  14%-noisy intent, seed 42) and of the baseline, on release 5. Save per-query top-10 ids.
- The DEV labels are for future tuning only. The verdict uses TEST.

## Decision rule (fixed now)
- If the MAIN-scope consensus metrics on TEST meet recall@5 ≥ 0.90, MRR@10 ≥ 0.75 and nDCG@5 ≥ 0.80:
  the targets are met under the TDD definition. Record this in specs and EVAL_RESULTS, and the system
  work becomes optional.
- Otherwise: report the gap per metric and per intent family, and propose system improvements tuned on
  DEV with the consensus labels.
