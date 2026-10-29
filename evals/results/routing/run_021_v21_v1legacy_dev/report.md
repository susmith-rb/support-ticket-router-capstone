# Routing Evaluation Report — Run `run_021_v21_v1legacy_dev`

- **Target**: `policy`
- **Generated At**: `2026-10-28T22:52:28.042285+00:00`
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
| **Routing Correctness (Primary)** | n/a (n=0) | n/a | 0.5048 (n=519) | [0.4577, 0.5460] |
| **Intent Accuracy** | 0.5833 (n=96) | [0.4660, 0.6979] | 0.6590 (n=519) | [0.6185, 0.7004] |
| **Lenient Intent Accuracy** | 0.5833 (n=96) | n/a | 0.6590 (n=519) | n/a |
| **Macro-F1** | 0.6711 (n=96) | [0.5178, 0.8188] | 0.7278 (n=519) | [0.6841, 0.7707] |
| **Decision Accuracy** | n/a (n=0) | n/a | 0.6281 (n=519) | [0.5874, 0.6680] |
| **Escalation Recall** | n/a (n=0) | n/a | 0.6953 (n=233) | n/a |
| **Strict Safety Recall (Wilson LB)** | n/a (n=0) | n/a | 1.0000 (LB: 0.9434) (n=64) | [1.0000, 1.0000] |
| **Degraded Rate** | 0.0000 (n=108) | n/a | 0.0000 (n=519) | n/a |
| **Error Rate** | 0.0000 (n=108) | n/a | 0.0000 (n=519) | n/a |
| **Auto-Resolution Share** | 0.3704 (n=108) | n/a | 0.4701 (n=519) | n/a |

## 2. Hard Gates Checklist

| Gate Name | Threshold | Actual Value | Status | Detail |
|---|---|---|---|---|
| `cost_cap` | `5.0` | `0.278126` | ✅ PASS | Total run cost $0.2781 vs cap $5.00 |
| `degraded_rate` | `0.01` | `0.0` | ✅ PASS | Degraded rate 0.0000 vs max 0.0100 |
| `error_rate` | `0.01` | `0.0` | ✅ PASS | Error rate 0.0000 vs max 0.0100 |
| `priority_gaming_violations` | `0` | `0` | ✅ PASS | 0 priority gaming violations vs max 0 |
| `prompt_injection_violations` | `0` | `0` | ✅ PASS | 0 prompt injection violations vs max 0 |
| `regression_pass_rate` | `1.0` | n/a | ⚠️ NOT_EVALUATED | run regression split |
| `strict_safety_recall` | `0.98` | n/a | ⚠️ NOT_EVALUATED | Strict safety recall not evaluated: no verified strict safety cases (n=0) |
| `taxonomy_validity` | `1.0` | `0.7534` | ❌ FAIL | Taxonomy validity rate 0.7534 vs min 1.0000 (invalid: shipping 41 tier2_flash, technical 40 tier2_flash, billing 21 tier2_flash, human_escalation 19 tier0_rules, refund 7 tier2_flash) |

### Secondary Non-Gating Targets

| Target Name | Threshold | Actual Value | Met | Detail |
|---|---|---|---|---|
| `cost_per_1k` | `0.5` | `0.4998` | ✅ YES | Cost per 1k tickets $0.4998 vs target $0.50 |
| `false_escalation` | `0.15` | `0.3951` | ⚠️ NO | False escalation rate 0.3951 vs target 0.1500 |
| `macro_f1` | `0.8` | `0.7278` | ⚠️ NO | Macro-F1 0.7278 vs target 0.8000 |
| `missed_escalation` | `0.02` | `0.3047` | ⚠️ NO | Missed escalation rate 0.3047 vs target 0.0200 |
| `paraphrase_consistency` | `0.95` | `0.8125` | ⚠️ NO | Paraphrase consistency 0.8125 vs target 0.9500 |
| `routing_p95_seconds` | `1.5` | `5.098` | ⚠️ NO | Policy p95 latency 5.098s vs target 1.500s |

## 3. Optimization Objective

- **Scenario-Stratified Objective (Verified)**: `n/a (n=0)`
- **Scenario-Stratified Objective (All)**: `0.4851`

## 4. Per-Category Intent Breakdown

| Intent | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| `account_payment` | 102 | 0.9054 | 0.6569 | 0.7614 |
| `order_status` | 109 | 0.8817 | 0.7523 | 0.8119 |
| `other` | 183 | 0.9194 | 0.6230 | 0.7427 |
| `returns_refunds` | 76 | 0.7407 | 0.7895 | 0.7643 |
| `shipping_policy` | 49 | 1.0000 | 0.3878 | 0.5588 |

## 5. Intent Confusion Matrix

| Expected \ Predicted | `account_payment` | `order_status` | `other` | `other_invalid` | `returns_refunds` | `shipping_policy` |
|---|---|---|---|---|---|---|
| `account_payment` | 67 | 1 | 3 | 30 | 1 | 0 |
| `order_status` | 2 | 82 | 2 | 17 | 6 | 0 |
| `other` | 4 | 5 | 114 | 50 | 10 | 0 |
| `returns_refunds` | 1 | 2 | 3 | 10 | 60 | 0 |
| `shipping_policy` | 0 | 3 | 2 | 21 | 4 | 19 |

## 6. Robustness & Security Integrity

- **Decision Consistency across Groups**: `0.8955`
- **Intent Consistency vs Parent**: `0.5682`
- **Paraphrase Consistency**: `0.8125`
- **Prompt Injection Violations**: `0`
- **Priority Gaming Violations**: `0`
- **Conflicting Information Accuracy**: `0.1905`

## 7. Stochastic Tier-2 Reliability (k-pass)

| Field | Krippendorff's Alpha | Mean Pairwise Agreement | SD Cases | SD Passes | Status |
|---|---|---|---|---|---|
| `decision` | 0.9252 | 0.9695 | 0.0698 | 0.0033 | ✅ Reliable |
| `escalate` | 0.9252 | 0.9695 | 0.4407 | 0.0033 | ✅ Reliable |
| `intent` | 0.9692 | 0.9771 | 0.0608 | 0.0057 | ✅ Reliable |

Unstable cases count: `14`

## 8. Top Failure Clusters

### Error Type Summary

| Error Type | Deciding Tier | Count | Share |
|---|---|---|---|
| `false_escalation` | `tier1_router_s1` | 88 | 34.24% |
| `invalid_label` | `tier2_flash` | 87 | 33.85% |
| `missed_escalation` | `tier2_flash` | 42 | 16.34% |
| `missed_oos` | `tier2_flash` | 9 | 3.50% |
| `wrong_intent` | `tier1_router_s1` | 9 | 3.50% |
| `missed_escalation` | `tier1_router_s1` | 7 | 2.72% |
| `invalid_label` | `tier0_rules` | 6 | 2.33% |
| `false_escalation` | `tier2_flash` | 4 | 1.56% |
| `wrong_queue` | `tier1_router_s1` | 4 | 1.56% |
| `wrong_intent` | `tier2_flash` | 1 | 0.39% |

### Top Failure Clusters

| Rank | Error Type | Transition | Deciding Tier | Size | Severity | Score | Signature Terms |
|---|---|---|---|---|---|---|---|
| 1 | `invalid_label` | `*->shipping|billing|refund|technical` | `tier2_flash` | 87 | 3 | 261 | `user, url, address, order, shipping` |
| 2 | `missed_escalation` | `other->other` | `tier2_flash` | 42 | 5 | 210 | `user, help, url, dm, amazon` |
| 3 | `false_escalation` | `order_status->order_status` | `tier1_router_s1` | 35 | 2 | 70 | `user, order, url, delivery, delivered` |
| 4 | `false_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 19 | 2 | 38 | `user, bloody, payment, help, need` |
| 5 | `false_escalation` | `returns_refunds->returns_refunds` | `tier1_router_s1` | 17 | 2 | 34 | `user, return, order, one, day` |
| 6 | `missed_oos` | `other->other` | `tier2_flash` | 9 | 2 | 18 | `sell, hey, good, quick, would` |
| 7 | `invalid_label` | `*->human_escalation` | `tier0_rules` | 6 | 3 | 18 | `fucking, address, delivery, change, wanna` |
| 8 | `missed_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 3 | 5 | 15 | `amazon, card, emi, wire, credit` |
| 9 | `missed_escalation` | `other->returns_refunds` | `tier1_router_s1` | 2 | 5 | 10 | `user, need, url, being, bulk` |
| 10 | `false_escalation` | `order_status->returns_refunds` | `tier1_router_s1` | 4 | 2 | 8 | `return, order, delivery, request, tracking` |

### Representative Failure Tickets

#### Cluster 1 (`invalid_label` — `*->shipping|billing|refund|technical`)
- **rt-kaggle_twcs-0f120f60**: "Amazon when are you going to start accepting Bitcoin as payment?? Thanks @user @user @user @user @user @user" (Expected: `auto_rag/account_payment`, Predicted: `auto_rag/billing`, Tier: `tier2_flash`)
- **rt-kaggle_twcs-7856eca3**: "Hi @user do you accept Bitcoin?" (Expected: `auto_rag/account_payment`, Predicted: `auto_rag/billing`, Tier: `tier2_flash`)

#### Cluster 2 (`missed_escalation` — `other->other`)
- **rt-kaggle_twcs-58452888**: "@user can you please add Taking Chance (2009) to the catalogue ?? I love this movie, but it isn't available anywhere! Amazon Prime (US) has it available! Can you help a user out ? @user @user @user I'" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)
- **rt-kaggle_twcs-279ccfb6**: "@user Please add Dangal Movie." (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)

#### Cluster 3 (`false_escalation` — `order_status->order_status`)
- **rt-synthetic_jetski-250e3eb9**: "@user Order [ORDER] past delivery date, tracking has no updates and seller not answering. How do I check where the shipment is?" (Expected: `auto_rag/order_status`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-synthetic_jetski-0342b66a**: "Hey @user, hoping you can help with order [ORDER]. The estimated arrival date was last week and it still hasn't shown up, plus the tracking info never updated. I tried messaging the seller with no res" (Expected: `auto_rag/order_status`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

### Representative Misclassified Tickets

#### `false_escalation`
- **rt-bitext_cs-0d84db1a**: "I want to check the bloody payment options" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)
- **rt-bitext_cs-186ff8ae**: "I need to submit the goddamn shipping address, how to do it?" (Expected: `auto_rag/shipping_policy`, Predicted: `escalate/other`, Tier: `tier1_router_s1`)
- **rt-bitext_cs-2533236e**: "I need help opening a new goddamn gold account" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)

#### `invalid_label`
- **rt-bitext_cs-0bd9d05c**: "I have entered a wrong fucking delivery address, correct it" (Expected: `auto_rag/shipping_policy`, Predicted: `escalate/human_escalation`, Tier: `tier0_rules`)
- **rt-bitext_cs-0f368673**: "I don't know how I could locate the bill #12588" (Expected: `auto_rag/account_payment`, Predicted: `auto_rag/billing`, Tier: `tier2_flash`)
- **rt-bitext_cs-16f0427e**: "could you help me editing my fucking account?" (Expected: `auto_rag/account_payment`, Predicted: `escalate/human_escalation`, Tier: `tier0_rules`)

#### `missed_escalation`
- **rt-bitext_cs-20b1aec0**: "where could I buy several articles?" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)
- **rt-bitext_cs-2d67a0de**: "assistance to submit feedback for ur company" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)
- **rt-bitext_cs-384f272a**: "earning something" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)

#### `wrong_intent`
- **rt-kaggle_twcs-0d90f116**: "@user not sure I'd say outside my front door is my safe space this is a regular theme guessing new drivers again . Done same to my neighbour to [URL]" (Expected: `auto_rag/shipping_policy`, Predicted: `auto_rag/order_status`, Tier: `tier1_router_s1`)
- **rt-kaggle_twcs-327b5033**: "@user My Kindle Fire is defective [URL]" (Expected: `auto_rag/other`, Predicted: `auto_rag/returns_refunds`, Tier: `tier1_router_s1`)
- **rt-kaggle_twcs-469362a4**: "@user I'm a Prime customer &amp; ordered immediately a book promoted by you as having dropped to $1.99 only to be charged its full price. Confused. Thanks for help!" (Expected: `auto_rag/other`, Predicted: `auto_rag/account_payment`, Tier: `tier1_router_s1`)

#### `wrong_queue`
- **rt-kaggle_twcs-9ff89778**: "Now,I will take legal actions against Amazon India and its' courier [URL] dare the courier boys behave rudely at my own" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-synthetic_jetski-1705f2e7**: "Compliance complaint regarding persistent failure of marketing SMS opt-out mechanism. Mobile number registered to account [EMAIL]. Customer transmitted standard regulatory opt-out command 'STOP' to me" (Expected: `escalate/other`, Predicted: `escalate/other`, Tier: `tier1_router_s1`)
- **rt-synthetic_template-98d610c0**: "Checking on tracking for order [ORDER]. But wait, the delivery driver threatened me with physical violence and assaulted my neighbor!" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

## 9. Cost & Latency Performance

- **Total Run Cost**: `$0.2781`
- **Decision Cost**: `$0.2594`
- **Reliability Cost (k-pass)**: `$0.0188`
- **Cost per 1,000 Tickets**: `$0.4998`
- **Projected Monthly Cost (1M tickets)**: `$499.76`
- **Projected Monthly Cost (10M tickets)**: `$4997.59`
- **Policy Latency p50**: `0.001s` | **p95**: `5.098s` | **p99**: `7.869s` | **Max**: `11.610s`

### Per-Tier Latency Breakdown

| Tier | Calls | p50 Latency | p95 Latency | p99 Latency | Max Latency |
|---|---|---|---|---|---|
| `tier1_router_s1` | 500 | n/a* | n/a* | n/a* | n/a* |
| `tier2_flash` | 525 | 5426.08 ms | 8740.42 ms | 10829.77 ms | 11605.99 ms |

*Note: Tiers marked with `n/a*` lack measured live wall-clock latency (cached stubs or replay entries). Run with `--refresh-cache` against live endpoints for measured latency.*

## 10. Per-Slice Performance

### By Source Dataset

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `bitext_cs` | 80 | 0.4000 | 0.6250 | 0.6500 |
| `kaggle_twcs` | 281 | 0.5552 | 0.6121 | 0.6726 |
| `perturbation` | 16 | 0.6250 | 0.7500 | 0.4375 |
| `synthetic_jetski` | 112 | 0.4643 | 0.6339 | 0.6786 |
| `synthetic_template` | 30 | 0.4000 | 0.7000 | 0.6000 |

### By Scenario

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `ambiguous` | 22 | 0.6818 | 0.8636 | 0.6364 |
| `conflicting` | 21 | 0.1905 | 0.5714 | 0.3810 |
| `long` | 20 | 0.6500 | 0.8000 | 0.7500 |
| `missing_info` | 21 | 0.5714 | 0.7143 | 0.8095 |
| `noisy` | 20 | 0.6000 | 0.7000 | 0.7500 |
| `normal` | 358 | 0.5363 | 0.6313 | 0.6480 |
| `ood` | 25 | 0.1200 | 0.1600 | 0.8800 |
| `paraphrase` | 12 | 0.5833 | 0.6667 | 0.9167 |
| `perturbation` | 16 | 0.6250 | 0.7500 | 0.4375 |
| `prompt_injection` | 13 | 0.4615 | 0.4615 | 0.6154 |
| `rare` | 19 | 0.3158 | 0.7368 | 0.3684 |

### By Deciding Tier

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `tier0_rules` | 19 | 0.6842 | 0.6842 | 0.0000 |
| `tier1_router_s1` | 325 | 0.6677 | 0.7077 | 0.8523 |
| `tier2_flash` | 175 | 0.1829 | 0.4743 | 0.3714 |

### By Label Status

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `disputed` | 122 | 0.3689 | 0.4426 | 0.6639 |
| `provisional` | 289 | 0.5087 | 0.6609 | 0.6713 |
| `verified` | 108 | 0.6481 | 0.7500 | 0.6204 |

### By Laya Contamination (in-distribution collision)

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `clean` | 388 | 0.4768 | 0.6211 | 0.6546 |
| `contaminated` | 131 | 0.5878 | 0.6489 | 0.6718 |

## 11. Degraded / Error Cases

Zero degraded or error cases encountered.

