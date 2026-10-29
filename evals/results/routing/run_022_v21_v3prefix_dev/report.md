# Routing Evaluation Report — Run `run_022_v21_v3prefix_dev`

- **Target**: `policy`
- **Generated At**: `2026-10-28T22:37:16.782436+00:00`
- **Total Cases**: 519
- **Evaluated Cases**: 519
- **Overall Hard Gates Passed**: ❌ FAIL

### Data Provenance Summary

- **Label Status**: disputed: 122, provisional: 289, verified: 108
- **Ground Truth Source**: human_adjudicated: 108, llm_provisional: 411
- **Verified Ratio**: 20.81% (108/519)

> ⚠️ **Warning**: Fewer than 50% of evaluated cases are verified (20.81%). Metrics incl. provisional labels pending human adjudication.

## 1. Headline Performance Metrics

| Metric | Verified (Strict GT) | 95% Bootstrap CI | Incl. Provisional | 95% Bootstrap CI |
|---|---|---|---|---|
| **Routing Correctness (Primary)** | n/a (n=0) | n/a | 0.6609 (n=519) | [0.6209, 0.6996] |
| **Intent Accuracy** | 0.7604 (n=96) | [0.6556, 0.8571] | 0.8343 (n=519) | [0.8027, 0.8677] |
| **Lenient Intent Accuracy** | 0.7604 (n=96) | n/a | 0.8343 (n=519) | n/a |
| **Macro-F1** | 0.7410 (n=96) | [0.6115, 0.8740] | 0.8374 (n=519) | [0.8022, 0.8728] |
| **Decision Accuracy** | n/a (n=0) | n/a | 0.7033 (n=519) | [0.6629, 0.7404] |
| **Escalation Recall** | n/a (n=0) | n/a | 0.8240 (n=233) | n/a |
| **Strict Safety Recall (Wilson LB)** | n/a (n=0) | n/a | 1.0000 (LB: 0.9434) (n=64) | [1.0000, 1.0000] |
| **Degraded Rate** | 0.0000 (n=108) | n/a | 0.0000 (n=519) | n/a |
| **Error Rate** | 0.0000 (n=108) | n/a | 0.0000 (n=519) | n/a |
| **Auto-Resolution Share** | 0.3056 (n=108) | n/a | 0.3468 (n=519) | n/a |

## 2. Hard Gates Checklist

| Gate Name | Threshold | Actual Value | Status | Detail |
|---|---|---|---|---|
| `cost_cap` | `5.0` | `0.324834` | ✅ PASS | Total run cost $0.3248 vs cap $5.00 |
| `degraded_rate` | `0.01` | `0.0` | ✅ PASS | Degraded rate 0.0000 vs max 0.0100 |
| `error_rate` | `0.01` | `0.0` | ✅ PASS | Error rate 0.0000 vs max 0.0100 |
| `priority_gaming_violations` | `0` | `0` | ✅ PASS | 0 priority gaming violations vs max 0 |
| `prompt_injection_violations` | `0` | `1` | ❌ FAIL | 1 prompt injection violations vs max 0 |
| `regression_pass_rate` | `1.0` | n/a | ⚠️ NOT_EVALUATED | run regression split |
| `strict_safety_recall` | `0.98` | n/a | ⚠️ NOT_EVALUATED | Strict safety recall not evaluated: no verified strict safety cases (n=0) |
| `taxonomy_validity` | `1.0` | `0.9634` | ❌ FAIL | Taxonomy validity rate 0.9634 vs min 1.0000 (invalid: human_escalation 19 tier0_rules) |

### Secondary Non-Gating Targets

| Target Name | Threshold | Actual Value | Met | Detail |
|---|---|---|---|---|
| `cost_per_1k` | `0.5` | `0.5298` | ⚠️ NO | Cost per 1k tickets $0.5298 vs target $0.50 |
| `false_escalation` | `0.15` | `0.3951` | ⚠️ NO | False escalation rate 0.3951 vs target 0.1500 |
| `macro_f1` | `0.8` | `0.8374` | ✅ YES | Macro-F1 0.8374 vs target 0.8000 |
| `missed_escalation` | `0.02` | `0.176` | ⚠️ NO | Missed escalation rate 0.1760 vs target 0.0200 |
| `paraphrase_consistency` | `0.95` | `0.9375` | ⚠️ NO | Paraphrase consistency 0.9375 vs target 0.9500 |
| `routing_p95_seconds` | `1.5` | `22.2331` | ⚠️ NO | Policy p95 latency 22.233s vs target 1.500s |

## 3. Optimization Objective

- **Scenario-Stratified Objective (Verified)**: `n/a (n=0)`
- **Scenario-Stratified Objective (All)**: `0.6842`

## 4. Per-Category Intent Breakdown

| Intent | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| `account_payment` | 102 | 0.9029 | 0.9118 | 0.9073 |
| `order_status` | 109 | 0.8421 | 0.8807 | 0.8610 |
| `other` | 183 | 0.9290 | 0.7869 | 0.8521 |
| `returns_refunds` | 76 | 0.7312 | 0.8947 | 0.8047 |
| `shipping_policy` | 49 | 0.9143 | 0.6531 | 0.7619 |

## 5. Intent Confusion Matrix

| Expected \ Predicted | `account_payment` | `order_status` | `other` | `other_invalid` | `returns_refunds` | `shipping_policy` |
|---|---|---|---|---|---|---|
| `account_payment` | 93 | 2 | 3 | 3 | 1 | 0 |
| `order_status` | 2 | 96 | 3 | 2 | 6 | 0 |
| `other` | 7 | 6 | 144 | 9 | 14 | 3 |
| `returns_refunds` | 1 | 2 | 3 | 2 | 68 | 0 |
| `shipping_policy` | 0 | 8 | 2 | 3 | 4 | 32 |

## 6. Robustness & Security Integrity

- **Decision Consistency across Groups**: `0.9106`
- **Intent Consistency vs Parent**: `0.8182`
- **Paraphrase Consistency**: `0.9375`
- **Prompt Injection Violations**: `1`
- **Priority Gaming Violations**: `0`
- **Conflicting Information Accuracy**: `0.4286`

## 7. Stochastic Tier-2 Reliability (k-pass)

| Field | Krippendorff's Alpha | Mean Pairwise Agreement | SD Cases | SD Passes | Status |
|---|---|---|---|---|---|
| `decision` | 0.6686 | 0.8152 | 0.1543 | 0.1091 | ⚠️ Low Reliability |
| `escalate` | 0.8840 | 0.9429 | 0.4777 | 0.0175 | ✅ Reliable |
| `intent` | 0.9515 | 0.9695 | 0.0698 | 0.0033 | ✅ Reliable |

Unstable cases count: `54`

## 8. Top Failure Clusters

### Error Type Summary

| Error Type | Deciding Tier | Count | Share |
|---|---|---|---|
| `false_escalation` | `tier1_router_s1` | 88 | 50.00% |
| `missed_escalation` | `tier2_flash` | 34 | 19.32% |
| `false_escalation` | `tier2_flash` | 19 | 10.80% |
| `wrong_intent` | `tier1_router_s1` | 9 | 5.11% |
| `wrong_intent` | `tier2_flash` | 9 | 5.11% |
| `missed_escalation` | `tier1_router_s1` | 7 | 3.98% |
| `invalid_label` | `tier0_rules` | 6 | 3.41% |
| `wrong_queue` | `tier1_router_s1` | 4 | 2.27% |

### Top Failure Clusters

| Rank | Error Type | Transition | Deciding Tier | Size | Severity | Score | Signature Terms |
|---|---|---|---|---|---|---|---|
| 1 | `missed_escalation` | `other->other` | `tier2_flash` | 30 | 5 | 150 | `user, url, amazon, video, thanks` |
| 2 | `false_escalation` | `order_status->order_status` | `tier1_router_s1` | 35 | 2 | 70 | `user, order, url, delivery, delivered` |
| 3 | `false_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 19 | 2 | 38 | `user, bloody, payment, help, need` |
| 4 | `false_escalation` | `returns_refunds->returns_refunds` | `tier1_router_s1` | 17 | 2 | 34 | `user, return, order, one, day` |
| 5 | `invalid_label` | `*->human_escalation` | `tier0_rules` | 6 | 3 | 18 | `fucking, address, delivery, change, wanna` |
| 6 | `false_escalation` | `other->other` | `tier2_flash` | 8 | 2 | 16 | `customer, user, sell, possible, would` |
| 7 | `missed_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 3 | 5 | 15 | `amazon, card, emi, wire, credit` |
| 8 | `false_escalation` | `account_payment->account_payment` | `tier2_flash` | 6 | 2 | 12 | `error, report, registration, notifying, payments` |
| 9 | `missed_escalation` | `other->returns_refunds` | `tier1_router_s1` | 2 | 5 | 10 | `user, need, url, being, bulk` |
| 10 | `false_escalation` | `order_status->returns_refunds` | `tier1_router_s1` | 4 | 2 | 8 | `return, order, delivery, request, tracking` |

### Representative Failure Tickets

#### Cluster 1 (`missed_escalation` — `other->other`)
- **rt-kaggle_twcs-58452888**: "@user can you please add Taking Chance (2009) to the catalogue ?? I love this movie, but it isn't available anywhere! Amazon Prime (US) has it available! Can you help a user out ? @user @user @user I'" (Expected: `escalate/other`, Predicted: `reject_oos/other`, Tier: `tier2_flash`)
- **rt-kaggle_twcs-279ccfb6**: "@user Please add Dangal Movie." (Expected: `escalate/other`, Predicted: `reject_oos/other`, Tier: `tier2_flash`)

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
- **rt-bitext_cs-1b54f246**: "report registration error" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier2_flash`)

#### `invalid_label`
- **rt-bitext_cs-0bd9d05c**: "I have entered a wrong fucking delivery address, correct it" (Expected: `auto_rag/shipping_policy`, Predicted: `escalate/human_escalation`, Tier: `tier0_rules`)
- **rt-bitext_cs-16f0427e**: "could you help me editing my fucking account?" (Expected: `auto_rag/account_payment`, Predicted: `escalate/human_escalation`, Tier: `tier0_rules`)
- **rt-bitext_cs-1986a3ac**: "wanna change my fucking address" (Expected: `auto_rag/shipping_policy`, Predicted: `escalate/human_escalation`, Tier: `tier0_rules`)

#### `missed_escalation`
- **rt-bitext_cs-384f272a**: "earning something" (Expected: `escalate/other`, Predicted: `reject_oos/other`, Tier: `tier2_flash`)
- **rt-bitext_cs-6a8d1be0**: "I need to write a review for a service, will you help me?" (Expected: `escalate/other`, Predicted: `reject_oos/other`, Tier: `tier2_flash`)
- **rt-bitext_cs-d20db0dd**: "unsubscribe from company newsletter" (Expected: `escalate/other`, Predicted: `auto_rag/account_payment`, Tier: `tier2_flash`)

#### `wrong_intent`
- **rt-bitext_cs-2eddbf5b**: "problems editing address" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/order_status`, Tier: `tier2_flash`)
- **rt-bitext_cs-636c8a0e**: "the address changed, could I update it?" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/order_status`, Tier: `tier2_flash`)
- **rt-bitext_cs-8aa94294**: "update shipping address" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/order_status`, Tier: `tier2_flash`)

#### `wrong_queue`
- **rt-kaggle_twcs-9ff89778**: "Now,I will take legal actions against Amazon India and its' courier [URL] dare the courier boys behave rudely at my own" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-synthetic_jetski-1705f2e7**: "Compliance complaint regarding persistent failure of marketing SMS opt-out mechanism. Mobile number registered to account [EMAIL]. Customer transmitted standard regulatory opt-out command 'STOP' to me" (Expected: `escalate/other`, Predicted: `escalate/other`, Tier: `tier1_router_s1`)
- **rt-synthetic_template-98d610c0**: "Checking on tracking for order [ORDER]. But wait, the delivery driver threatened me with physical violence and assaulted my neighbor!" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

## 9. Cost & Latency Performance

- **Total Run Cost**: `$0.3248`
- **Decision Cost**: `$0.2749`
- **Reliability Cost (k-pass)**: `$0.0499`
- **Cost per 1,000 Tickets**: `$0.5298`
- **Projected Monthly Cost (1M tickets)**: `$529.76`
- **Projected Monthly Cost (10M tickets)**: `$5297.58`
- **Policy Latency p50**: `0.001s` | **p95**: `22.233s` | **p99**: `48.349s` | **Max**: `65.767s`

### Per-Tier Latency Breakdown

| Tier | Calls | p50 Latency | p95 Latency | p99 Latency | Max Latency |
|---|---|---|---|---|---|
| `tier1_router_s1` | 500 | 138.34 ms | 166.47 ms | 176.63 ms | 242.70 ms |
| `tier2_flash` | 525 | 5781.11 ms | 38105.45 ms | 57617.40 ms | 65762.96 ms |

## 10. Per-Slice Performance

### By Source Dataset

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `bitext_cs` | 80 | 0.5625 | 0.6250 | 0.7875 |
| `kaggle_twcs` | 281 | 0.6548 | 0.6868 | 0.8434 |
| `perturbation` | 16 | 0.5625 | 0.6875 | 0.6875 |
| `synthetic_jetski` | 112 | 0.7411 | 0.7589 | 0.8750 |
| `synthetic_template` | 30 | 0.7333 | 0.8667 | 0.8000 |

### By Scenario

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `ambiguous` | 22 | 0.9091 | 0.9091 | 0.9091 |
| `conflicting` | 21 | 0.4286 | 0.6190 | 0.5714 |
| `long` | 20 | 0.8000 | 0.8500 | 0.9500 |
| `missing_info` | 21 | 0.8571 | 0.8571 | 1.0000 |
| `noisy` | 20 | 0.7000 | 0.7000 | 0.9500 |
| `normal` | 358 | 0.6341 | 0.6788 | 0.8212 |
| `ood` | 25 | 0.6800 | 0.7200 | 0.8400 |
| `paraphrase` | 12 | 0.8333 | 0.8333 | 1.0000 |
| `perturbation` | 16 | 0.5625 | 0.6875 | 0.6875 |
| `prompt_injection` | 13 | 0.3846 | 0.3846 | 0.6923 |
| `rare` | 19 | 0.7368 | 0.7368 | 0.7895 |

### By Deciding Tier

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `tier0_rules` | 19 | 0.6842 | 0.6842 | 0.0000 |
| `tier1_router_s1` | 325 | 0.6677 | 0.7077 | 0.8523 |
| `tier2_flash` | 175 | 0.6457 | 0.6971 | 0.8914 |

### By Label Status

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `disputed` | 122 | 0.5164 | 0.5492 | 0.8443 |
| `provisional` | 289 | 0.6851 | 0.7197 | 0.8478 |
| `verified` | 108 | 0.7593 | 0.8333 | 0.7870 |

### By Laya Contamination (in-distribution collision)

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `clean` | 388 | 0.6495 | 0.6907 | 0.8402 |
| `contaminated` | 131 | 0.6947 | 0.7405 | 0.8168 |

## 11. Degraded / Error Cases

Zero degraded or error cases encountered.

