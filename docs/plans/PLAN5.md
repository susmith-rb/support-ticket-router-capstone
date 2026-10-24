# PLAN5: improve retrieval ranking under TDD-conformant labels (2026-10-24)

The user approved the improvement plan and authorized a stronger adjudicator (Gemini Pro).

## Labels amendment (decided BEFORE any C2-based score is computed)
- Adjudicator C2 is a Jetski Gemini Pro agent (`Model: pro`), with the same blindness rules as C. It sees
  queries, the catalog and A/B labels, and NO system output or scores. It re-adjudicates all 600 queries,
  with a stricter relevance instruction: a secondary counts only if a support agent would cite it as a
  correct answer on its own, not merely as related.
- C2 becomes the consensus for PLAN5 decisions, on label-quality grounds and at the user's request.
- C results remain reported alongside, along with C-vs-C2 agreement (Jaccard of relevant sets, primary
  match). If C2 relevant sets are smaller, expect lower recall and MRR; that is the honest direction.

## Experiments
All tuning uses DEV with the consensus labels and the MAIN scope (English, support, answerable).
TEST gets a single audited run at the end for the finalists.
1. Intent boost: {0, 0.1, 0.2} with noisy intent. Keep the boost only if it improves DEV consensus nDCG@5
   without hurting primary-only recall@5.
2. Reranking for graded nDCG, using the Ranking API (`semantic-ranker-512@latest` and any newer listed
   model) on the top 20. Records carry RICH content: title + "Also asked as" questions + the first ~600
   chars of the answer body. Compare rerank-all-20 against rerank-then-blend (keep the original top 1
   unless the reranker score margin exceeds a threshold tuned on DEV). Record latency and cost.
3. Runtime query rewrite. Gemini Flash (gemini-3.8-flash@global, a runtime-path call billed to Argolis,
   which is allowed) rewrites the raw tweet into one clear customer question with no handles or URLs. Then
   run the search, with and without the original query appended. Measure tokens, $ per 1k queries and
   added latency.
4. Only if 1–3 leave nDCG below 0.80 on DEV: sharpen overlapping sibling articles (order_status,
   returns). This is agent-authored, leakage-safe, and goes through the publish gate.

## Decision rule
- Finalists (≤ 3), picked by DEV consensus nDCG@5, with recall@5 ≥ 0.90 and MRR ≥ 0.75 as constraints.
- Run one audited TEST run and report it against the TDD targets, with the lower/upper bounds and the
  C-based numbers.
- Any added runtime cost or latency must stay within p95 ≤ 400 ms for the search span. The query rewrite
  counts against the overall path budget (TDD §10 timeout table), and is reported separately.
