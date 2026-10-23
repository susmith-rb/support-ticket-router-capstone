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

## Reproduce

```bash
# Tier 1
python rag/retrieval_eval.py --threshold LOWEST --clean        # see retrieval_sweep.sh for all configs
# Tier 2 + 3 (writes questions/retrieval/answers/judge jsonl + report.json into <dir>)
python rag/clean_eval.py <dir> <faq_docs_dir>
python rag/clean_eval_breakdown.py <dir>                       # abstention + unsupported claims
python rag/evalsvc_groundedness.py <dir> <faq_docs_dir>        # Eval Service, explicit autorater
```
