# Routing Evaluation Report — Run `run_014_v2_cand1_dev`

- **Target**: `policy`
- **Generated At**: `2026-10-28T22:40:37.451998+00:00`
- **Total Cases**: 519
- **Evaluated Cases**: 519
- **Overall Hard Gates Passed**: ❌ FAIL

### Data Provenance Summary

- **Label Status**: disputed: 114, provisional: 309, verified: 96
- **Ground Truth Source**: human_adjudicated: 96, llm_provisional: 423
- **Verified Ratio**: 18.50% (96/519)

> ⚠️ **Warning**: Fewer than 50% of evaluated cases are verified (18.50%). Metrics incl. provisional labels pending human adjudication.

## 1. Headline Performance Metrics

| Metric | Verified (Strict GT) | 95% Bootstrap CI | Incl. Provisional | 95% Bootstrap CI |
|---|---|---|---|---|
| **Routing Correctness (Primary)** | n/a (n=0) | n/a | 0.5857 (n=519) | [0.5411, 0.6267] |
| **Intent Accuracy** | 0.7917 (n=96) | [0.6990, 0.8812] | 0.8459 (n=519) | [0.8157, 0.8770] |
| **Lenient Intent Accuracy** | 0.7917 (n=96) | n/a | 0.8459 (n=519) | n/a |
| **Macro-F1** | 0.7657 (n=96) | [0.6256, 0.9120] | 0.8470 (n=519) | [0.8106, 0.8829] |
| **Decision Accuracy** | n/a (n=0) | n/a | 0.6224 (n=519) | [0.5795, 0.6634] |
| **Escalation Recall** | n/a (n=0) | n/a | 0.6583 (n=240) | n/a |
| **Strict Safety Recall (Wilson LB)** | n/a (n=0) | n/a | 1.0000 (LB: 0.9434) (n=64) | [1.0000, 1.0000] |
| **Degraded Rate** | 0.0000 (n=96) | n/a | 0.0000 (n=519) | n/a |
| **Error Rate** | 0.0000 (n=96) | n/a | 0.0000 (n=519) | n/a |
| **Auto-Resolution Share** | 0.3646 (n=96) | n/a | 0.4759 (n=519) | n/a |

## 2. Hard Gates Checklist

| Gate Name | Threshold | Actual Value | Status | Detail |
|---|---|---|---|---|
| `cost_cap` | `5.0` | `0.284278` | ✅ PASS | Total run cost $0.2843 vs cap $5.00 |
| `degraded_rate` | `0.01` | `0.0` | ✅ PASS | Degraded rate 0.0000 vs max 0.0100 |
| `error_rate` | `0.01` | `0.0` | ✅ PASS | Error rate 0.0000 vs max 0.0100 |
| `priority_gaming_violations` | `0` | `0` | ✅ PASS | 0 priority gaming violations vs max 0 |
| `prompt_injection_violations` | `0` | `0` | ✅ PASS | 0 prompt injection violations vs max 0 |
| `regression_pass_rate` | `1.0` | n/a | ⚠️ NOT_EVALUATED | run regression split |
| `strict_safety_recall` | `0.98` | n/a | ⚠️ NOT_EVALUATED | Strict safety recall not evaluated: no verified strict safety cases (n=0) |
| `taxonomy_validity` | `1.0` | `0.9634` | ❌ FAIL | Taxonomy validity rate 0.9634 vs min 1.0000 (invalid: human_escalation 19 tier0_rules) |

### Secondary Non-Gating Targets

| Target Name | Threshold | Actual Value | Met | Detail |
|---|---|---|---|---|
| `cost_per_1k` | `0.5` | `0.5037` | ⚠️ NO | Cost per 1k tickets $0.5037 vs target $0.50 |
| `false_escalation` | `0.15` | `0.4086` | ⚠️ NO | False escalation rate 0.4086 vs target 0.1500 |
| `macro_f1` | `0.8` | `0.847` | ✅ YES | Macro-F1 0.8470 vs target 0.8000 |
| `missed_escalation` | `0.02` | `0.3417` | ⚠️ NO | Missed escalation rate 0.3417 vs target 0.0200 |
| `paraphrase_consistency` | `0.95` | `0.875` | ⚠️ NO | Paraphrase consistency 0.8750 vs target 0.9500 |
| `routing_p95_seconds` | `1.5` | `5.9982` | ⚠️ NO | Policy p95 latency 5.998s vs target 1.500s |

## 3. Optimization Objective

- **Scenario-Stratified Objective (Verified)**: `n/a (n=0)`
- **Scenario-Stratified Objective (All)**: `0.5497`

## 4. Per-Category Intent Breakdown

| Intent | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| `account_payment` | 102 | 0.9200 | 0.9020 | 0.9109 |
| `order_status` | 109 | 0.8636 | 0.8716 | 0.8676 |
| `other` | 183 | 0.9107 | 0.8361 | 0.8718 |
| `returns_refunds` | 76 | 0.7556 | 0.8947 | 0.8193 |
| `shipping_policy` | 49 | 0.9688 | 0.6327 | 0.7654 |

## 5. Intent Confusion Matrix

| Expected \ Predicted | `account_payment` | `order_status` | `other` | `other_invalid` | `returns_refunds` | `shipping_policy` |
|---|---|---|---|---|---|---|
| `account_payment` | 92 | 1 | 4 | 3 | 1 | 1 |
| `order_status` | 2 | 95 | 4 | 2 | 6 | 0 |
| `other` | 5 | 5 | 153 | 9 | 11 | 0 |
| `returns_refunds` | 1 | 2 | 3 | 2 | 68 | 0 |
| `shipping_policy` | 0 | 7 | 4 | 3 | 4 | 31 |

## 6. Robustness & Security Integrity

- **Decision Consistency across Groups**: `0.9182`
- **Intent Consistency vs Parent**: `0.8485`
- **Paraphrase Consistency**: `0.8750`
- **Prompt Injection Violations**: `0`
- **Priority Gaming Violations**: `0`
- **Conflicting Information Accuracy**: `0.3810`

## 7. Stochastic Tier-2 Reliability (k-pass)

| Field | Krippendorff's Alpha | Mean Pairwise Agreement | SD Cases | SD Passes | Status |
|---|---|---|---|---|---|
| `decision` | 0.8833 | 0.9543 | 0.0845 | 0.0114 | ✅ Reliable |
| `escalate` | 0.8833 | 0.9543 | 0.4259 | 0.0175 | ✅ Reliable |
| `intent` | 0.9664 | 0.9810 | 0.0557 | 0.0033 | ✅ Reliable |

Unstable cases count: `17`

## 8. Top Failure Clusters

### Error Type Summary

| Error Type | Deciding Tier | Count | Share |
|---|---|---|---|
| `false_escalation` | `tier1_router_s1` | 91 | 42.33% |
| `missed_escalation` | `tier2_flash` | 75 | 34.88% |
| `false_escalation` | `tier2_flash` | 18 | 8.37% |
| `wrong_intent` | `tier1_router_s1` | 9 | 4.19% |
| `missed_escalation` | `tier1_router_s1` | 7 | 3.26% |
| `wrong_intent` | `tier2_flash` | 6 | 2.79% |
| `invalid_label` | `tier0_rules` | 5 | 2.33% |
| `wrong_queue` | `tier1_router_s1` | 4 | 1.86% |

### Top Failure Clusters

| Rank | Error Type | Transition | Deciding Tier | Size | Severity | Score | Signature Terms |
|---|---|---|---|---|---|---|---|
| 1 | `missed_escalation` | `other->other` | `tier2_flash` | 72 | 5 | 360 | `user, help, url, amazon, would` |
| 2 | `false_escalation` | `order_status->order_status` | `tier1_router_s1` | 35 | 2 | 70 | `user, order, url, delivery, delivered` |
| 3 | `false_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 19 | 2 | 38 | `user, bloody, payment, help, need` |
| 4 | `false_escalation` | `returns_refunds->returns_refunds` | `tier1_router_s1` | 17 | 2 | 34 | `user, return, order, one, day` |
| 5 | `invalid_label` | `*->human_escalation` | `tier0_rules` | 5 | 3 | 15 | `fucking, address, delivery, change, wanna` |
| 6 | `missed_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 3 | 5 | 15 | `amazon, card, emi, wire, credit` |
| 7 | `false_escalation` | `order_status->order_status` | `tier2_flash` | 6 | 2 | 12 | `user, url, guys, today, deliver` |
| 8 | `missed_escalation` | `other->returns_refunds` | `tier1_router_s1` | 2 | 5 | 10 | `user, need, url, being, bulk` |
| 9 | `false_escalation` | `order_status->returns_refunds` | `tier1_router_s1` | 4 | 2 | 8 | `return, order, delivery, request, tracking` |
| 10 | `false_escalation` | `other->other` | `tier2_flash` | 4 | 2 | 8 | `user, bookmyshow, game, horrible, quality` |

### Representative Failure Tickets

#### Cluster 1 (`missed_escalation` — `other->other`)
- **rt-kaggle_twcs-58452888**: "@user can you please add Taking Chance (2009) to the catalogue ?? I love this movie, but it isn't available anywhere! Amazon Prime (US) has it available! Can you help a user out ? @user @user @user I'" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)
- **rt-kaggle_twcs-279ccfb6**: "@user Please add Dangal Movie." (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)

#### Cluster 2 (`false_escalation` — `order_status->order_status`)
- **rt-synthetic_jetski-250e3eb9**: "@user Order [ORDER] past delivery date, tracking has no updates and seller not answering. How do I check where the shipment is?" (Expected: `auto_rag/order_status`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-synthetic_jetski-0342b66a**: "Hey @user, hoping you can help with order [ORDER]. The estimated arrival date was last week and it still hasn't shown up, plus the tracking info never updated. I tried messaging the seller with no res" (Expected: `auto_rag/order_status`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

#### Cluster 3 (`false_escalation` — `account_payment->account_payment`)
- **rt-bitext_cs-3819e70f**: "I want help to see what bloody payment options are accepted" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)
- **rt-bitext_cs-0d84db1a**: "I want to check the bloody payment options" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)

### Representative Misclassified Tickets

#### `false_escalation`
- **rt-bitext_cs-0d84db1a**: "I want to check the bloody payment options" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)
- **rt-bitext_cs-186ff8ae**: "I need to submit the goddamn shipping address, how to do it?" (Expected: `auto_rag/shipping_policy`, Predicted: `escalate/other`, Tier: `tier1_router_s1`)
- **rt-bitext_cs-2533236e**: "I need help opening a new goddamn gold account" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)

#### `invalid_label`
- **rt-bitext_cs-0bd9d05c**: "I have entered a wrong fucking delivery address, correct it" (Expected: `auto_rag/shipping_policy`, Predicted: `escalate/human_escalation`, Tier: `tier0_rules`)
- **rt-bitext_cs-16f0427e**: "could you help me editing my fucking account?" (Expected: `auto_rag/account_payment`, Predicted: `escalate/human_escalation`, Tier: `tier0_rules`)
- **rt-bitext_cs-1986a3ac**: "wanna change my fucking address" (Expected: `auto_rag/shipping_policy`, Predicted: `escalate/human_escalation`, Tier: `tier0_rules`)

#### `missed_escalation`
- **rt-bitext_cs-20b1aec0**: "where could I buy several articles?" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)
- **rt-bitext_cs-2d67a0de**: "assistance to submit feedback for ur company" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)
- **rt-bitext_cs-384f272a**: "earning something" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)

#### `wrong_intent`
- **rt-bitext_cs-636c8a0e**: "the address changed, could I update it?" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/order_status`, Tier: `tier2_flash`)
- **rt-bitext_cs-8aa94294**: "update shipping address" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/order_status`, Tier: `tier2_flash`)
- **rt-bitext_cs-cb013829**: "what do I need to do to change my delivery address?" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/order_status`, Tier: `tier2_flash`)

#### `wrong_queue`
- **rt-kaggle_twcs-9ff89778**: "Now,I will take legal actions against Amazon India and its' courier [URL] dare the courier boys behave rudely at my own" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-synthetic_jetski-1705f2e7**: "Compliance complaint regarding persistent failure of marketing SMS opt-out mechanism. Mobile number registered to account [EMAIL]. Customer transmitted standard regulatory opt-out command 'STOP' to me" (Expected: `escalate/other`, Predicted: `escalate/other`, Tier: `tier1_router_s1`)
- **rt-synthetic_template-98d610c0**: "Checking on tracking for order [ORDER]. But wait, the delivery driver threatened me with physical violence and assaulted my neighbor!" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

## 9. Cost & Latency Performance

- **Total Run Cost**: `$0.2843`
- **Decision Cost**: `$0.2614`
- **Reliability Cost (k-pass)**: `$0.0229`
- **Cost per 1,000 Tickets**: `$0.5037`
- **Projected Monthly Cost (1M tickets)**: `$503.71`
- **Projected Monthly Cost (10M tickets)**: `$5037.08`
- **Policy Latency p50**: `0.001s` | **p95**: `5.998s` | **p99**: `11.291s` | **Max**: `53.063s`

### Per-Tier Latency Breakdown

| Tier | Calls | p50 Latency | p95 Latency | p99 Latency | Max Latency |
|---|---|---|---|---|---|
| `tier1_router_s1` | 500 | n/a* | n/a* | n/a* | n/a* |
| `tier2_flash` | 525 | 6536.91 ms | 14829.29 ms | 37609.04 ms | 53058.58 ms |

*Note: Tiers marked with `n/a*` lack measured live wall-clock latency (cached stubs or replay entries). Run with `--refresh-cache` against live endpoints for measured latency.*

## 10. Per-Slice Performance

### By Source Dataset

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `bitext_cs` | 80 | 0.6000 | 0.6375 | 0.8250 |
| `kaggle_twcs` | 281 | 0.5836 | 0.6121 | 0.8505 |
| `perturbation` | 16 | 0.5000 | 0.5625 | 0.7500 |
| `synthetic_jetski` | 112 | 0.5893 | 0.6161 | 0.8750 |
| `synthetic_template` | 30 | 0.6000 | 0.7333 | 0.8000 |

### By Scenario

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `ambiguous` | 22 | 0.8636 | 0.8636 | 0.9091 |
| `conflicting` | 21 | 0.3810 | 0.5714 | 0.5714 |
| `long` | 20 | 0.7000 | 0.7500 | 0.9500 |
| `missing_info` | 21 | 0.7143 | 0.7143 | 1.0000 |
| `noisy` | 20 | 0.7000 | 0.7000 | 0.9500 |
| `normal` | 358 | 0.5922 | 0.6257 | 0.8352 |
| `ood` | 25 | 0.2400 | 0.2800 | 0.8800 |
| `paraphrase` | 12 | 0.4167 | 0.4167 | 1.0000 |
| `perturbation` | 16 | 0.5000 | 0.5625 | 0.7500 |
| `prompt_injection` | 13 | 0.3077 | 0.3077 | 0.6923 |
| `rare` | 19 | 0.6316 | 0.6842 | 0.7895 |

### By Deciding Tier

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `tier0_rules` | 19 | 0.7368 | 0.7368 | 0.0000 |
| `tier1_router_s1` | 325 | 0.6585 | 0.6985 | 0.8523 |
| `tier2_flash` | 175 | 0.4343 | 0.4686 | 0.9257 |

### By Label Status

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `disputed` | 114 | 0.4474 | 0.4825 | 0.8333 |
| `provisional` | 309 | 0.6149 | 0.6440 | 0.8673 |
| `verified` | 96 | 0.6562 | 0.7188 | 0.7917 |

### By Laya Contamination (in-distribution collision)

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `clean` | 388 | 0.5799 | 0.6160 | 0.8531 |
| `contaminated` | 131 | 0.6031 | 0.6412 | 0.8244 |

## 11. Degraded / Error Cases

Zero degraded or error cases encountered.

## 12. Baseline vs Candidate Comparison

### Verdict: ❌ `REJECT_BLOCKED_PREEXISTING`

- Candidate failed hard gates.
- Gate NOT_EVALUATED: strict_safety_recall - Strict safety recall not evaluated: no verified strict safety cases (n=0)
- Gate NOT_EVALUATED: regression_pass_rate - run regression split
- Gate PRE_EXISTING: taxonomy_validity - Taxonomy validity rate 0.9634 vs min 1.0000 (invalid: human_escalation 19 tier0_rules)
- Improves the objective (delta=0.1052, 95% CI=[0.0623, 0.1556]) and introduces no new gate failures; blocked only by gates that also fail or are not evaluated in the baseline.

| Metric Delta | Point Estimate | 95% Paired-Bootstrap CI |
|---|---|---|
| `decision_accuracy` | n/a | [-0.0144, 0.0099] |
| `intent_accuracy` | n/a | [0.1515, 0.2231] |
| `objective` | 0.1052 | [0.0623, 0.1556] |
| `routing_correctness` | 0.0848 | [0.0589, 0.1100] |

- **McNemar Routing-Correctness p-value**: `0.000000`
- **McNemar Decision-Correctness p-value**: `1.000000`
- **Wins**: 48 | **Losses**: 4 | **Ties**: 467
- **Newly Failing Regression Cases**: `0`

