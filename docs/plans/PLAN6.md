# PLAN6: data-side disambiguation samples in the index (2026-10-24)

The user's decision: NO Agent Search tuning and NO model training. Generate samples and put them INTO THE INDEX as
distinguishing questions and scope notes per article, to lift graded nDCG@5 (TDD §6.4 ≥ 0.80).
Baseline: TEST nDCG@5 0.695 with C2 labels. The primary is at rank 1 for 56% of DEV queries; the rest of
the time a sibling is ranked first.

## What gets generated (Jetski agents only; no Vertex calls; no leakage)
1. Confusion map (DEV, production config, C2 labels): for every query where the primary is not at rank 1,
   record the pair (primary → doc ranked first). Take the pairs and articles with the most confusions,
   up to about 8 pairs and about 15 articles.
2. Sample pool: customer first turns from themes_v2 threads that are NOT in DEV or TEST (by thread_id),
   English, support-like, drawn from the themes of the target articles.
3. Agent labeling of the pool, blind to system output, GUIDE.md style: for each sampled tweet, primary +
   relevant from the catalog. About 40–60 labeled tweets per target article. This gives
   "generated samples" with labels from an agent's judgment, not silver theme membership.
4. Per target article, from the samples labeled to it:
   - 8–12 distinguishing "Also asked as" questions: paraphrased customer phrasing that captures what makes
     THIS article right and its sibling wrong (e.g. "my Prime 2-day order is late" vs "how fast is Prime
     delivery")
   - a short scope line at the top: "This article covers … For … see <sibling title>." No numbers, and
     publish lint must stay clean.
   - optionally drop existing generic questions that pull the sibling's queries (record which)
5. Provenance file: rag/corpus_v2/agent_decisions/disambiguation_v1.json with
   {doc_id: {questions, scope_line, removed_questions, source_thread_ids}}. Leakage tests as in
   paraphrases_v1.

## Gate and measurement
- Publish as release 6 (draft status, versions bump for touched docs) and verify live.
- DEV decision: score with the C2 multi-labels, MAIN scope, production config. Keep the release only if:
  - DEV nDCG@5 improves by ≥ 0.03 vs release 5 (0.729)
  - DEV recall@5 and MRR do not drop by more than 0.01
  - single-gold DEV recall@5 per intent does not drop by more than 0.05
  Otherwise roll back to release 5.
- If kept: ONE audited TEST run, then specs + EVAL_RESULTS §11, with an honest report vs the 0.80 target.
