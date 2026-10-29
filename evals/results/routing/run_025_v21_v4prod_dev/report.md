# Routing Evaluation Report — Run `run_025_v21_v4prod_dev`

- **Target**: `policy`
- **Generated At**: `2026-10-29T00:18:35.008947+00:00`
- **Total Cases**: 519
- **Evaluated Cases**: 519
- **Overall Hard Gates Passed**: ⚠️ INCOMPLETE

### Data Provenance Summary

- **Label Status**: disputed: 122, provisional: 289, verified: 108
- **Ground Truth Source**: human_adjudicated: 108, llm_provisional: 411
- **Verified Ratio**: 20.81% (108/519)

> ⚠️ **Warning**: Fewer than 50% of evaluated cases are verified (20.81%). Metrics incl. provisional labels pending human adjudication.

## 1. Headline Performance Metrics

| Metric | Verified (Strict GT) | 95% Bootstrap CI | Incl. Provisional | 95% Bootstrap CI |
|---|---|---|---|---|
| **Routing Correctness (Primary)** | n/a (n=0) | n/a | 0.7380 (n=519) | [0.6990, 0.7765] |
| **Intent Accuracy** | 0.8021 (n=96) | [0.7083, 0.8901] | 0.8651 (n=519) | [0.8361, 0.8956] |
| **Lenient Intent Accuracy** | 0.8021 (n=96) | n/a | 0.8651 (n=519) | n/a |
| **Macro-F1** | 0.7623 (n=96) | [0.6207, 0.9098] | 0.8500 (n=519) | [0.8121, 0.8878] |
| **Decision Accuracy** | n/a (n=0) | n/a | 0.7746 (n=519) | [0.7364, 0.8105] |
| **Escalation Recall** | n/a (n=0) | n/a | 0.9442 (n=233) | n/a |
| **Strict Safety Recall (Wilson LB)** | n/a (n=0) | n/a | 1.0000 (LB: 0.9434) (n=64) | [1.0000, 1.0000] |
| **Degraded Rate** | 0.0000 (n=108) | n/a | 0.0000 (n=519) | n/a |
| **Error Rate** | 0.0000 (n=108) | n/a | 0.0000 (n=519) | n/a |
| **Auto-Resolution Share** | 0.2870 (n=108) | n/a | 0.3526 (n=519) | n/a |

## 2. Hard Gates Checklist

| Gate Name | Threshold | Actual Value | Status | Detail |
|---|---|---|---|---|
| `cost_cap` | `5.0` | `0.363967` | ✅ PASS | Total run cost $0.3640 vs cap $5.00 |
| `degraded_rate` | `0.01` | `0.0` | ✅ PASS | Degraded rate 0.0000 vs max 0.0100 |
| `error_rate` | `0.01` | `0.0` | ✅ PASS | Error rate 0.0000 vs max 0.0100 |
| `priority_gaming_violations` | `0` | `0` | ✅ PASS | 0 priority gaming violations vs max 0 |
| `prompt_injection_violations` | `0` | `0` | ✅ PASS | 0 prompt injection violations vs max 0 |
| `regression_pass_rate` | `1.0` | n/a | ⚠️ NOT_EVALUATED | run regression split |
| `strict_safety_recall` | `0.98` | n/a | ⚠️ NOT_EVALUATED | Strict safety recall not evaluated: no verified strict safety cases (n=0) |
| `taxonomy_validity` | `1.0` | `1.0` | ✅ PASS | Taxonomy validity rate 1.0000 vs min 1.0000 |

### Secondary Non-Gating Targets

| Target Name | Threshold | Actual Value | Met | Detail |
|---|---|---|---|---|
| `cost_per_1k` | `0.5` | `0.5549` | ⚠️ NO | Cost per 1k tickets $0.5549 vs target $0.50 |
| `false_escalation` | `0.15` | `0.3636` | ⚠️ NO | False escalation rate 0.3636 vs target 0.1500 |
| `macro_f1` | `0.8` | `0.85` | ✅ YES | Macro-F1 0.8500 vs target 0.8000 |
| `missed_escalation` | `0.02` | `0.0558` | ⚠️ NO | Missed escalation rate 0.0558 vs target 0.0200 |
| `paraphrase_consistency` | `0.95` | `0.9375` | ⚠️ NO | Paraphrase consistency 0.9375 vs target 0.9500 |
| `routing_p95_seconds` | `1.5` | `13.8479` | ⚠️ NO | Policy p95 latency 13.848s vs target 1.500s |

## 3. Optimization Objective

- **Scenario-Stratified Objective (Verified)**: `n/a (n=0)`
- **Scenario-Stratified Objective (All)**: `0.7379`

## 4. Per-Category Intent Breakdown

| Intent | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| `account_payment` | 102 | 0.9109 | 0.9020 | 0.9064 |
| `order_status` | 109 | 0.8796 | 0.8716 | 0.8756 |
| `other` | 183 | 0.8670 | 0.8907 | 0.8787 |
| `returns_refunds` | 76 | 0.7473 | 0.8947 | 0.8144 |
| `shipping_policy` | 49 | 1.0000 | 0.6327 | 0.7750 |

## 5. Intent Confusion Matrix

| Expected \ Predicted | `account_payment` | `order_status` | `other` | `other_invalid` | `returns_refunds` | `shipping_policy` |
|---|---|---|---|---|---|---|
| `account_payment` | 92 | 1 | 7 | 0 | 2 | 0 |
| `order_status` | 2 | 95 | 6 | 0 | 6 | 0 |
| `other` | 4 | 5 | 163 | 0 | 11 | 0 |
| `returns_refunds` | 1 | 2 | 5 | 0 | 68 | 0 |
| `shipping_policy` | 2 | 5 | 7 | 0 | 4 | 31 |

## 6. Robustness & Security Integrity

- **Decision Consistency across Groups**: `0.9288`
- **Intent Consistency vs Parent**: `0.8485`
- **Paraphrase Consistency**: `0.9375`
- **Prompt Injection Violations**: `0`
- **Priority Gaming Violations**: `0`
- **Conflicting Information Accuracy**: `0.4286`

## 7. Stochastic Tier-2 Reliability (k-pass)

| Field | Krippendorff's Alpha | Mean Pairwise Agreement | SD Cases | SD Passes | Status |
|---|---|---|---|---|---|
| `decision` | 0.8766 | 0.9376 | 0.1045 | 0.0370 | ✅ Reliable |
| `escalate` | 0.9562 | 0.9786 | 0.4877 | 0.0111 | ✅ Reliable |
| `intent` | 0.9634 | 0.9786 | 0.0589 | 0.0093 | ✅ Reliable |

Unstable cases count: `21`

## 8. Top Failure Clusters

### Error Type Summary

| Error Type | Deciding Tier | Count | Share |
|---|---|---|---|
| `false_escalation` | `tier1_router_s1` | 79 | 58.09% |
| `false_escalation` | `tier2_flash` | 19 | 13.97% |
| `wrong_intent` | `tier1_router_s1` | 9 | 6.62% |
| `missed_escalation` | `tier1_router_s1` | 7 | 5.15% |
| `false_escalation` | `tier0_rules` | 6 | 4.41% |
| `missed_escalation` | `tier2_flash` | 6 | 4.41% |
| `wrong_intent` | `tier2_flash` | 6 | 4.41% |
| `wrong_queue` | `tier1_router_s1` | 4 | 2.94% |

### Top Failure Clusters

| Rank | Error Type | Transition | Deciding Tier | Size | Severity | Score | Signature Terms |
|---|---|---|---|---|---|---|---|
| 1 | `false_escalation` | `order_status->order_status` | `tier1_router_s1` | 31 | 2 | 62 | `user, order, url, delivery, delivered` |
| 2 | `false_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 17 | 2 | 34 | `bloody, user, payment, want, help` |
| 3 | `false_escalation` | `returns_refunds->returns_refunds` | `tier1_router_s1` | 16 | 2 | 32 | `user, return, order, one, day` |
| 4 | `false_escalation` | `other->other` | `tier2_flash` | 11 | 2 | 22 | `user, customer, sell, possible, would` |
| 5 | `missed_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 3 | 5 | 15 | `amazon, card, emi, wire, credit` |
| 6 | `missed_escalation` | `other->other` | `tier2_flash` | 3 | 5 | 15 | `url, accessibility, documentation, vpat, affiliate` |
| 7 | `missed_escalation` | `other->returns_refunds` | `tier1_router_s1` | 2 | 5 | 10 | `user, need, url, being, bulk` |
| 8 | `false_escalation` | `order_status->returns_refunds` | `tier1_router_s1` | 4 | 2 | 8 | `return, order, delivery, request, tracking` |
| 9 | `false_escalation` | `account_payment->account_payment` | `tier2_flash` | 3 | 2 | 6 | `error, got, report, sign, account` |
| 10 | `false_escalation` | `shipping_policy->other` | `tier0_rules` | 3 | 2 | 6 | `address, fucking, delivery, change, wanna` |

### Representative Failure Tickets

#### Cluster 1 (`false_escalation` — `order_status->order_status`)
- **rt-synthetic_jetski-0342b66a**: "Hey @user, hoping you can help with order [ORDER]. The estimated arrival date was last week and it still hasn't shown up, plus the tracking info never updated. I tried messaging the seller with no res" (Expected: `auto_rag/order_status`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-kaggle_twcs-c0552dee**: "Hey @user I think I might need some help. An order I placed in September with a deliver by date of 11/9 still hasn’t arrived (no tracking info). I have contacted the seller to no avail. Any suggestion" (Expected: `auto_rag/order_status`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

#### Cluster 2 (`false_escalation` — `account_payment->account_payment`)
- **rt-bitext_cs-3819e70f**: "I want help to see what bloody payment options are accepted" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)
- **rt-bitext_cs-0d84db1a**: "I want to check the bloody payment options" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)

#### Cluster 3 (`false_escalation` — `returns_refunds->returns_refunds`)
- **rt-perturbation-b97054bf**: "@user don't understand one day delivery meaning any more, deliver urgent product 2 days late and backs out when asked to return the product. How ?" (Expected: `auto_rag/returns_refunds`, Predicted: `escalate/returns_refunds`, Tier: `tier1_router_s1`)
- **rt-kaggle_twcs-3d4305fd**: "@user I paid extra for one day shipping. It’s been more than a day - can I get my money back??" (Expected: `auto_rag/returns_refunds`, Predicted: `escalate/returns_refunds`, Tier: `tier1_router_s1`)

### Representative Misclassified Tickets

#### `false_escalation`
- **rt-bitext_cs-0bd9d05c**: "I have entered a wrong fucking delivery address, correct it" (Expected: `auto_rag/shipping_policy`, Predicted: `escalate/other`, Tier: `tier0_rules`)
- **rt-bitext_cs-0d84db1a**: "I want to check the bloody payment options" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)
- **rt-bitext_cs-16f0427e**: "could you help me editing my fucking account?" (Expected: `auto_rag/account_payment`, Predicted: `escalate/other`, Tier: `tier0_rules`)

#### `missed_escalation`
- **rt-bitext_cs-d55829a9**: "would it be possible to lodge a reclamation?" (Expected: `escalate/other`, Predicted: `auto_rag/returns_refunds`, Tier: `tier2_flash`)
- **rt-kaggle_twcs-081b4ae6**: "My dad put my number on his @user account so I just got a text letting me know my Christmas gifts have shipped 😂" (Expected: `escalate/other`, Predicted: `auto_rag/order_status`, Tier: `tier1_router_s1`)
- **rt-kaggle_twcs-147c6cb7**: "Ordered the new Samsung galaxy 8 from @user. The phone is fine but you need to be careful with the packaging. Massive hole in the box it arrived in. @user [URL]" (Expected: `escalate/other`, Predicted: `auto_rag/returns_refunds`, Tier: `tier1_router_s1`)

#### `wrong_intent`
- **rt-bitext_cs-2eddbf5b**: "problems editing address" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/account_payment`, Tier: `tier2_flash`)
- **rt-bitext_cs-636c8a0e**: "the address changed, could I update it?" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/account_payment`, Tier: `tier2_flash`)
- **rt-bitext_cs-8aa94294**: "update shipping address" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/order_status`, Tier: `tier2_flash`)

#### `wrong_queue`
- **rt-kaggle_twcs-9ff89778**: "Now,I will take legal actions against Amazon India and its' courier [URL] dare the courier boys behave rudely at my own" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-synthetic_jetski-1705f2e7**: "Compliance complaint regarding persistent failure of marketing SMS opt-out mechanism. Mobile number registered to account [EMAIL]. Customer transmitted standard regulatory opt-out command 'STOP' to me" (Expected: `escalate/other`, Predicted: `escalate/other`, Tier: `tier1_router_s1`)
- **rt-synthetic_template-98d610c0**: "Checking on tracking for order [ORDER]. But wait, the delivery driver threatened me with physical violence and assaulted my neighbor!" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

## 9. Cost & Latency Performance

- **Total Run Cost**: `$0.3640`
- **Decision Cost**: `$0.2880`
- **Reliability Cost (k-pass)**: `$0.0760`
- **Cost per 1,000 Tickets**: `$0.5549`
- **Projected Monthly Cost (1M tickets)**: `$554.89`
- **Projected Monthly Cost (10M tickets)**: `$5548.86`
- **Policy Latency p50**: `0.000s` | **p95**: `13.848s` | **p99**: `35.898s` | **Max**: `42.146s`

### Per-Tier Latency Breakdown

| Tier | Calls | p50 Latency | p95 Latency | p99 Latency | Max Latency |
|---|---|---|---|---|---|
| `tier1_router_s1` | 500 | n/a* | n/a* | n/a* | n/a* |
| `tier2_flash` | 561 | 4590.78 ms | 24180.99 ms | 39665.22 ms | 42142.18 ms |

*Note: Tiers marked with `n/a*` lack measured live wall-clock latency (cached stubs or replay entries). Run with `--refresh-cache` against live endpoints for measured latency.*

## 10. Per-Slice Performance

### By Source Dataset

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `bitext_cs` | 80 | 0.6625 | 0.7125 | 0.8500 |
| `kaggle_twcs` | 281 | 0.7544 | 0.7794 | 0.8577 |
| `perturbation` | 16 | 0.6875 | 0.7500 | 0.7500 |
| `synthetic_jetski` | 112 | 0.7500 | 0.7679 | 0.9196 |
| `synthetic_template` | 30 | 0.7667 | 0.9333 | 0.8333 |

### By Scenario

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `ambiguous` | 22 | 0.8636 | 0.8636 | 0.9545 |
| `conflicting` | 21 | 0.4286 | 0.6667 | 0.5714 |
| `long` | 20 | 0.8500 | 0.9000 | 0.9500 |
| `missing_info` | 21 | 0.8571 | 0.8571 | 1.0000 |
| `noisy` | 20 | 0.7500 | 0.7500 | 0.9500 |
| `normal` | 358 | 0.7318 | 0.7654 | 0.8464 |
| `ood` | 25 | 0.8400 | 0.8800 | 1.0000 |
| `paraphrase` | 12 | 0.8333 | 0.8333 | 1.0000 |
| `perturbation` | 16 | 0.6875 | 0.7500 | 0.7500 |
| `prompt_injection` | 13 | 0.5385 | 0.5385 | 0.6923 |
| `rare` | 19 | 0.7368 | 0.7368 | 0.8947 |

### By Deciding Tier

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `tier0_rules` | 19 | 0.6842 | 0.6842 | 0.4737 |
| `tier1_router_s1` | 313 | 0.6837 | 0.7252 | 0.8530 |
| `tier2_flash` | 187 | 0.8342 | 0.8663 | 0.9251 |

### By Label Status

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `disputed` | 122 | 0.6066 | 0.6393 | 0.8607 |
| `provisional` | 289 | 0.7751 | 0.8097 | 0.8824 |
| `verified` | 108 | 0.7870 | 0.8333 | 0.8241 |

### By Laya Contamination (in-distribution collision)

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `clean` | 388 | 0.7320 | 0.7706 | 0.8763 |
| `contaminated` | 131 | 0.7557 | 0.7863 | 0.8321 |

## 11. Degraded / Error Cases

Zero degraded or error cases encountered.

## 12. Baseline vs Candidate Comparison

### Verdict: ❌ `REJECT_BLOCKED_PREEXISTING`

- Candidate failed hard gates.
- Gate NOT_EVALUATED: strict_safety_recall - Strict safety recall not evaluated: no verified strict safety cases (n=0)
- Gate NOT_EVALUATED: regression_pass_rate - run regression split
- Improves the objective (delta=0.0537, 95% CI=[0.0126, 0.0959]) and introduces no new gate failures; blocked only by gates that also fail or are not evaluated in the baseline.

| Metric Delta | Point Estimate | 95% Paired-Bootstrap CI |
|---|---|---|
| `decision_accuracy` | n/a | [0.0445, 0.1000] |
| `intent_accuracy` | n/a | [0.0119, 0.0483] |
| `objective` | 0.0537 | [0.0126, 0.0959] |
| `routing_correctness` | 0.0771 | [0.0521, 0.1043] |

- **McNemar Routing-Correctness p-value**: `0.000000`
- **McNemar Decision-Correctness p-value**: `0.000001`
- **Wins**: 48 | **Losses**: 8 | **Ties**: 463
- **Newly Failing Regression Cases**: `0`

