# RAG evaluation results (Agent Search, `support-kb-search`)

Date: 2026-10-23. Project `susmith-fde-ticket-router`. All numbers below are measured unless marked
**estimate**. Raw outputs: `rag/results/` (sweep) and `rag/results/clean_eval/` (clean eval).

## 1. What is indexed

| Data store | Content | How it was built | Cost |
|---|---|---|---|
| `faq-articles` | 53 FAQ articles (`corpus/faq-articles/v1/`) | `build_faq_corpus.py`: Gemini 3.1 Pro preview clustered the 81k customer-opened threads' topics into 56 themes; 3 "positive feedback" themes dropped (`drop_articles.sh`); each article synthesized from up to 15 real threads plus one canonical `SIMULATED_POLICY` (single-sourced, `POLICY_VERSION`) | ≈ \$1.30 measured tokens |
| `resolved-threads` | 81,103 structured thread docs (`corpus/resolved-threads/v1/`) | `build_threads_corpus.py` from Kaggle `thoughtvector/customer-support-on-twitter` (redacted) | n/a (no LLM) |

Search app `support-kb-search`: Enterprise tier + LLM add-on, location `global`.

> [!IMPORTANT]
> The 53 articles are LLM-synthesized and **not yet human-reviewed** (all `status=draft`). The
> publish lint already found one invented fact ("respond within 48 hours" in
> `faq-account-payment-account-locked-blocked`). Fix it before the articles go active.

## 2. Tier 1: real-tweet retrieval (hardest, most realistic)

The queries are 300 held-out real customer tweets (noisy, @mentions, slang). Their labels are silver: an
article counts as relevant if it was built from the tweet's theme. Script: `retrieval_eval.py`, sweep: `retrieval_sweep.sh`.

| Config | Zero-result queries | recall@5 | MRR |
|---|---|---|---|
| Default (`relevanceThreshold` LOW) | 113 | 0.467 | 0.384 |
| `LOWEST` | 9 | 0.750 | 0.551 |
| **`LOWEST` + query cleaning** (strip @mentions / [TOKENS] / #) | 9 | **0.760** (r@10 0.84) | **0.562** |
| + query expansion | — | no gain | — |

recall@5 by intent family: account_payment 0.78, order_status 0.82, returns_refunds 0.86,
shipping 0.78, **other 0.59** (the weakest family, and the target of the index-side work in §7).

## 3. Tier 2: clean-question retrieval (comparable to typical RAG evals)

Script: `clean_eval.py`. Claude Sonnet 5.5 wrote 106 clean customer questions, two per article. The gold
answer for each is the article it was written from. Each question goes through `support-kb-search`
(`LOWEST` threshold).

| n | recall@1 | recall@3 | recall@5 | recall@10 | MRR@10 | zero-result |
|---|---|---|---|---|---|---|
| 106 | 0.519 | 0.726 | **0.840** | 0.934 | 0.650 | 0 |

Most misses happen between overlapping articles. For example, a "2 days past estimate" question
written from `order-not-shipped` returns `late-general-delivery` and `general-order-not-arrived`
first, which is a defensible answer. See `results/clean_eval/retrieval_misses.json`. Because of
this overlap, recall@1 understates real usefulness, and it also motivates merging the overlapping
articles (§7).

## 4. Tier 3: answer groundedness (comparable to Daniel's "Grounding")

Gemini 3.8 Flash answers each of the 106 questions from the top-5 retrieved docs. It returns structured
JSON with an answer, verbatim-quote citations, and an `escalate`/`grounded` flag.

### 4a. Deterministic checks (no LLM)

| Metric | Value |
|---|---|
| Answers with ≥1 citation | 99.1% |
| Citation precision (quote appears verbatim in the cited doc) | **100%** (376 citations) |
| Answer cites the source article | 72.6% |
| Abstained / escalated | 24.5% (26/106) |

Abstention breakdown:
- The model escalated in 21 of 89 cases where the source article was in the top-5.
- It escalated in 5 of 17 cases where the source was not in the top-5.

Most of the 21 follow article text such as "connect the customer with a specialist when the customer
accidentally shipped to an old address". The model obeys the article's escalation rule, so these
are not retrieval failures. Of the 80 non-escalated answers, 57 cite the source article. The rest
cite an overlapping article.

### 4b. LLM judge: atomic-claim support (Claude Opus 5.5)

| Metric | Value |
|---|---|
| Claims extracted | 559 |
| **Claim support rate** | **97.7%** |
| Faithfulness (1–5) | 4.83 |
| Answers with every claim supported | 88.7% |

The unsupported claims are mostly small inferences, not invented policy. Examples:
- "since it's 2 days past estimate it's officially late"
- "a specialist will help right away"
- "it is common for packages to be dispatched closer to the delivery date"

### 4c. Vertex Gen AI Evaluation Service

- The built-in `groundedness` metric **failed**. Its fixed default autorater
  (`gemini-2.5-flash`, us-central1) returns 404 in this project, and that metric rejects
  `autoraterConfig` (106/106 errors). These failures are recorded in `results/clean_eval/evalsvc.jsonl`.
- Workaround: `evalsvc_groundedness.py`. It uses `evaluateInstances` with a `pointwiseMetricInput`, a
  groundedness rubric (1–5), and autorater `gemini-3.8-flash` at `global`. Result: **106/106 scored, 0 errors, mean 4.98 / 5, 104 scored 5 and 2 scored 4 (100% ≥ 4)**.
  The autorater is more lenient than the Opus atomic-claim judge: 98% of answers get a 5, but only 88.7% are
  fully supported claim-by-claim. Treat the Opus judge (97.7%) as the headline number and the
  Eval Service as a managed-service cross-check. The service is also wired into the pipeline for CI.

## 5. Comparison with Daniel's deck and the baseline TDD

| | Daniel | Ours |
|---|---|---|
| Corpus | "789 verified store policy chunks" (origin unstated) | 53 synthesized articles + 81k real threads, built from the Kaggle data with one canonical simulated policy |
| Eval questions | 44 hand-written, against his own chunks | 106 LLM-written (one source article each) **plus** 300 real held-out tweets |
| Retrieval recall@k | not reported | clean recall@5 **0.84**; real-tweet recall@5 **0.76** |
| Grounding / faithfulness | **95.5%** "Grounding" | **97.7%** claim support (Opus judge); 100% verbatim citation precision; Vertex Eval Service groundedness 4.98/5 (100% ≥4) |

The baseline `support_tdd` prescribes:
- indexing tweet→reply QA pairs (300-token chunks);
- gates of FAQ grounding recall ≥85% and ROUGE-L ≥0.35 / BLEU ≥0.30 against the historical reply.

We chose not to gate on ROUGE/BLEU against Twitter replies. Those replies are mostly "please DM us", so
copying them would score well and help nobody. Groundedness and citation precision measure what the
customer actually receives.

**Can we match Daniel?** On the comparable metric (answer groundedness on clean questions), yes:
97.7% vs 95.5%. Our extra tier is retrieval on real tweets, and there we are at 0.76 recall@5. That
is below our own TDD G5 target (0.90), and the honest gap to report.

## 6. Cost

| Step | Model | Input tokens | Output tokens |
|---|---|---|---|
| Question generation | claude-sonnet-5-5 | 44,087 | 5,341 |
| Answers | gemini-3.8-flash | 303,533 | 165,551 |
| Judge | claude-opus-5-5 | 429,618 | 49,492 |
| Eval Service | gemini-3.8-flash (autorater) | not metered by script | |

**Estimate:** a few dollars total for one full clean-eval run. Token counts are measured; the dollar
figure is an estimate from list prices.

Search queries: about 410 per full eval (106 clean + 300 real + probes). On the current Enterprise +
LLM add-on tier that is ≈ \$3.3 per run (**estimate**, \$8/1k). Standard tier would be \$1.50/1k.

## 7. Caveats and next steps

Caveats:
- Real-tweet labels are silver (theme membership), not human-judged.
- Articles are unreviewed LLM syntheses. Clean questions were generated from the same articles, so
  Tier 2 and Tier 3 are optimistic upper bounds. Tier 1 is the realistic one.
- recall@5 on real tweets is 0.76, which is below the TDD G5 target of 0.90.

Next steps (index side):
1. Regroup the `other` theme by topic (recall@5 0.59).
2. Merge overlapping articles: late delivery / late Hub Plus delivery / order not arrived / not shipped.
3. Add articles for uncovered topics (devices, Prime Video, app issues).
4. Re-import with `publish_faq.py` (FULL reconciliation), then rerun
   `retrieval_eval.py --threshold LOWEST --clean` and `clean_eval.py`. Gate: Δrecall@5 ≥ −0.02 overall.
5. Pending decisions: switch `support-kb-search` to Standard with no LLM add-on (our agent generates
   the answers), human review of the 53 articles, and a second golden-set labeler.

## 8. Corpus v2 (index-side improvements), now LIVE as release 3

**What changed** (`rag/corpus_v2/`: agent decisions, themes_v2, gold map, manifest). There are 50
articles; 44 are byte-identical to v1.
- Merged:
  - late general / late Hub Plus / order not arrived / order not shipped → `faq-order-status-late-or-not-arrived-order`
  - missing delivered / stolen → `faq-order-status-missing-or-stolen-delivered-package`
- Split devices: Kindle & e-readers, Fire TV & streaming devices, and general device & hardware.
- Fixed the invented "48 hours" fact in `faq-account-payment-account-locked-blocked`.
- Dropped 4 themes: 3 praise themes and social-media chatter (91 threads with no support need).
- **Authorship:** the 6 new or changed articles and the regroup decisions were written by a Jetski
  agent (Gemini 3.8 Flash, high thinking), and each carries the provenance field
  `authored_by: jetski-agent:gemini-3.8-flash-high`. Vertex tokens used: 0. Like v1, they are not yet
  human-reviewed.

**A bug the first publish exposed.** `publish_faq.py` imported retired tombstones, so the replaced
articles stayed in the index and outranked their merged successors. Real-tweet recall@5 fell
−0.031, the gate failed, and the publish was automatically rolled back to v1. The fix:
- The manifest keeps the tombstones for audit.
- The import payload now excludes `status=retired`, so a FULL reconciliation deletes them.
- Post-import verification asserts that a retired id is not returned.
- Both behaviors have unit tests. In the rerun, retired ids appear in 0 of 900 top-3 slots.

**v1 → v2 (release 3).** Same 300 real tweets and same 106 clean questions; v1 golds are mapped
through `gold_map_v1_v2.json`.

| Set | Metric | v1 | v2 | Δ |
|---|---|---|---|---|
| Real tweets, excluding the 13 dropped-gold queries (n=287; **gate set**) | recall@5 | 0.787 | 0.780 | −0.007 ✅ (gate ≥ −0.02) |
| | order_status recall@5 | 0.817 | **0.845** | +0.028 |
| | account_payment / other / returns / shipping recall@5 | 0.783 / 0.671 / 0.864 / 0.778 | 0.804 / 0.658 / 0.818 / 0.778 | +0.021 / −0.013 / −0.046 / 0 |
| Real tweets, excluding dropped and merged golds (n=242) | order_status recall@5 | 0.731 | **0.808** | +0.077 |
| Clean questions (n=106) | recall@1 / @3 / @5 / @10 | 0.519 / 0.726 / 0.840 / 0.934 | **0.557 / 0.783** / 0.821 / **0.943** | +0.038 / +0.057 / −0.019 / +0.009 |
| | MRR@10 | 0.650 | **0.681** | +0.031 |
| | verbatim citation precision | 100% | 100% (338 citations, re-verified) | 0 |
| | abstained / escalated | 24.5% | **19.8%** | −4.7 pts |

**Reading it:**
- The merges did what they were meant to. order_status improved, and the "excluding merged" slice
  shows the gain isn't just easier gold matching.
- The `other` split did **not** move recall: 0.671 → 0.658, which is within noise.
- returns_refunds fell 0.046 even though none of its articles changed. That is 4 of 88 queries, and
  it gives the **ranker-noise floor** from republishing the corpus.
- At n≈290, a −0.02 gate is close to that noise. Next step: average over 2–3 query samples or use a
  paired bootstrap CI before tightening the gate.
- The Opus claim judge and the Eval Service were not rerun for v2. Per the billing rule, offline
  judging now runs on Jetski agents. For B, v2 relies on the deterministic citation checks.

Remaining gap: real-tweet recall@5 is 0.78, against the G5 target of 0.90. The weakest family is
`other` (0.66).

## 9. TDD §6.3 filters, §6.4 ablations, recall push (2026-10-24; PLAN3)

Method:
- Discipline: the 300 real-tweet queries are now the **frozen TEST** set. A new disjoint **DEV** set of
  300 is used for all tuning. TEST was run only for pre-approved finalists, and every run is audited in
  `results/v3/test_runs.log`.
- Metrics are on slice b (dropped golds excluded). Latency is client-side from the cloudtop.

What changed, all committed:
- `rag/search_client.py`:
  - status filter (`FAQ_ALLOWED_STATUSES` defaults to active+draft until review) and effective_from filter
  - intent filter with an unfiltered fallback
  - soft intent `boostSpec`
  - the Ranking API rerank, for experiments only
- A `publish_faq` metadata bug was fixed. New articles had lost `intent_family` in the index; release 4
  fixed that.
- 109 agent-authored paraphrases were added to the 14 most-missed articles (release 5). Their source
  threads are outside both DEV and TEST, which a test enforces.

| Experiment (DEV, n=288) | r@5 | Note |
|---|---|---|
| Baseline (LOWEST + clean) | 0.781 | |
| Threshold LOW | 0.479 | drops too much |
| Managed rerank (semantic-ranker-512, top 20 → 5) | 0.594 | +93 ms; not used |
| rankingExpression blends | unsupported | needs `embedding_spec` |
| Hard intent filter, oracle / 14%-noisy intent | 0.896 / 0.750 | brittle under router error |
| **Soft intent boost 0.2**, oracle / noisy | 0.875 / **0.840** | chosen |
| + paraphrases (release 5), noisy | **0.882** | |

**Frozen TEST (n=287), production config = LOWEST + clean + soft intent boost 0.2 with 14%-noisy intent:**

| | Baseline, release 4 | Production, release 4 | **Production, release 5** | TDD target |
|---|---|---|---|---|
| recall@1 | 0.415 | 0.488 | **0.519** | — |
| recall@5 | 0.780 | 0.822 | **0.836** | ≥ 0.90 |
| recall@10 | 0.875 | 0.913 | **0.927** | — |
| MRR@10 | 0.570 | 0.626 | **0.652** | ≥ 0.75 |
| nDCG@5 | 0.613 | 0.666 | **0.689** | ≥ 0.80 |
| p95 latency | 282 ms | 263 ms | **251 ms** | ≤ 400 ms |

Release 5 production recall@5 by intent:
- account_payment 0.848
- order_status 0.873
- other 0.753
- returns_refunds 0.864
- shipping_policy 0.889 (n=9)

Reading it:
- From the original baseline to production on release 5, recall@5 rose +0.056 and MRR +0.082.
- The paraphrase gain shrank from DEV (+0.042) to TEST (+0.014). That's expected, because the articles
  were chosen from DEV misses; the TEST figure is the honest one.
- **The 0.90 / 0.75 / 0.80 targets are still not met.** `other` remains the weakest family. Remaining
  levers:
  - split `other` further
  - paraphrases chosen by family instead of from DEV misses
  - real router intent instead of simulated noise
  - query rewrite (a runtime Gemini call)
  - custom embeddings, which would unlock rankingExpression

## Reproduce

```bash
# Tier 1
python rag/retrieval_eval.py --threshold LOWEST --clean        # see retrieval_sweep.sh for all configs
# Tier 2 + 3 (writes questions/retrieval/answers/judge jsonl + report.json into <dir>)
python rag/clean_eval.py <dir> <faq_docs_dir>
python rag/clean_eval_breakdown.py <dir>                       # abstention + unsupported claims
python rag/evalsvc_groundedness.py <dir> <faq_docs_dir>        # Eval Service, explicit autorater
```

## 10. TDD-conformant measurement (PLAN4/PLAN5, 2026-10-24)

The TDD §6.4 `retrieval/v1` set is now built and checked in at `rag/labels/retrieval_v1/`:
- 600 queries (TEST + DEV), labeled blind by 2 Jetski annotators (κ primary 0.84).
- Adjudicated by 4 Gemini Pro shards (C2), with a keep/drop verdict for every candidate article.
- Validated by a blind Opus expert on 50 stratified queries: its primary is inside the consensus
  relevant set 38/38 times, and its scores match.

TEST, MAIN scope (English support questions that an article can answer, n=225), production config:

| TDD §6.4 target | Measured | |
|---|---|---|
| recall@5 ≥ 0.90 | 0.907 (primary-only 0.831; expert sample 0.906) | met, narrowly |
| MRR@10 ≥ 0.75 | 0.784 (primary-only 0.645; expert sample 0.779) | met |
| graded nDCG@5 ≥ 0.80 | 0.695 (expert sample 0.708) | **not met** |
| filtered vs unfiltered Δrecall@5 ≥ −0.02 | +0.005 (soft boost vs none) | met |
| p95 search latency ≤ 400 ms | ~250 ms (client-side) | met |

DEV ablations under these labels (`results/v3/phaseD_dev_multilabel_C2.json`):
- Ranking API rerank hurts in every form.
- Gemini Flash query rewrite brings no significant gain, at p95 23 s.
- The intent boost is within noise of no boost.

nDCG is limited by sibling-article confusion. The primary is at rank 1 for 56% of queries. The top pair
is prime-delivery-speed-delays vs late-or-not-arrived-order, an overlap created by the v2 merge.
Next lever: sharpen overlapping articles (PLAN5, experiment 4).
