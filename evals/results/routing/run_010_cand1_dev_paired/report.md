# Routing Evaluation Report — Run `run_010_cand1_dev_paired`

- **Target**: `policy`
- **Generated At**: `2026-10-28T20:52:51.736290+00:00`
- **Total Cases**: 519
- **Evaluated Cases**: 519
- **Overall Hard Gates Passed**: ❌ FAIL

### Data Provenance Summary

- **Label Status**: disputed: 120, provisional: 309, verified: 90
- **Ground Truth Source**: human_adjudicated: 90, llm_provisional: 429
- **Verified Ratio**: 17.34% (90/519)

> ⚠️ **Warning**: Fewer than 50% of evaluated cases are verified (17.34%). Metrics incl. provisional labels pending human adjudication.

## 1. Headline Performance Metrics

| Metric | Verified (Strict GT) | 95% Bootstrap CI | Incl. Provisional | 95% Bootstrap CI |
|---|---|---|---|---|
| **Routing Correctness (Primary)** | n/a (n=0) | n/a | 0.5742 (n=519) | [0.5315, 0.6174] |
| **Intent Accuracy** | 0.7778 (n=90) | [0.6739, 0.8706] | 0.8150 (n=519) | [0.7795, 0.8485] |
| **Lenient Intent Accuracy** | 0.7778 (n=90) | n/a | 0.8150 (n=519) | n/a |
| **Macro-F1** | 0.7314 (n=90) | [0.6078, 0.8968] | 0.8243 (n=519) | [0.7833, 0.8592] |
| **Decision Accuracy** | n/a (n=0) | n/a | 0.6185 (n=519) | [0.5742, 0.6634] |
| **Escalation Recall** | n/a (n=0) | n/a | 0.6245 (n=229) | n/a |
| **Strict Safety Recall (Wilson LB)** | n/a (n=0) | n/a | 0.9839 (LB: 0.9141) (n=62) | [0.9516, 1.0000] |
| **Degraded Rate** | 0.0000 (n=90) | n/a | 0.0000 (n=519) | n/a |
| **Error Rate** | 0.0000 (n=90) | n/a | 0.0000 (n=519) | n/a |
| **Auto-Resolution Share** | 0.3667 (n=90) | n/a | 0.5087 (n=519) | n/a |

## 2. Hard Gates Checklist

| Gate Name | Threshold | Actual Value | Status | Detail |
|---|---|---|---|---|
| `cost_cap` | `5.0` | `0.284199` | ✅ PASS | Total run cost $0.2842 vs cap $5.00 |
| `degraded_rate` | `0.01` | `0.0` | ✅ PASS | Degraded rate 0.0000 vs max 0.0100 |
| `error_rate` | `0.01` | `0.0` | ✅ PASS | Error rate 0.0000 vs max 0.0100 |
| `priority_gaming_violations` | `0` | `0` | ✅ PASS | 0 priority gaming violations vs max 0 |
| `prompt_injection_violations` | `0` | `0` | ✅ PASS | 0 prompt injection violations vs max 0 |
| `regression_pass_rate` | `1.0` | n/a | ⚠️ NOT_EVALUATED | run regression split |
| `strict_safety_recall` | `0.98` | n/a | ⚠️ NOT_EVALUATED | Strict safety recall not evaluated: no verified strict safety cases (n=0) |
| `taxonomy_validity` | `1.0` | `0.9576` | ❌ FAIL | Taxonomy validity rate 0.9576 vs min 1.0000 (invalid: human_escalation 22 tier0_rules) |

### Secondary Non-Gating Targets

| Target Name | Threshold | Actual Value | Met | Detail |
|---|---|---|---|---|
| `cost_per_1k` | `0.5` | `0.5017` | ⚠️ NO | Cost per 1k tickets $0.5017 vs target $0.50 |
| `false_escalation` | `0.15` | `0.3862` | ⚠️ NO | False escalation rate 0.3862 vs target 0.1500 |
| `macro_f1` | `0.8` | `0.8243` | ✅ YES | Macro-F1 0.8243 vs target 0.8000 |
| `missed_escalation` | `0.02` | `0.3755` | ⚠️ NO | Missed escalation rate 0.3755 vs target 0.0200 |
| `paraphrase_consistency` | `0.95` | `0.75` | ⚠️ NO | Paraphrase consistency 0.7500 vs target 0.9500 |
| `routing_p95_seconds` | `1.5` | `0.0082` | ✅ YES | Policy p95 latency 0.008s vs target 1.500s |

## 3. Optimization Objective

- **Scenario-Stratified Objective (Verified)**: `n/a (n=0)`
- **Scenario-Stratified Objective (All)**: `0.5622`

## 4. Per-Category Intent Breakdown

| Intent | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| `account_payment` | 102 | 0.9184 | 0.8824 | 0.9000 |
| `order_status` | 110 | 0.8000 | 0.8727 | 0.8348 |
| `other` | 169 | 0.8710 | 0.7988 | 0.8333 |
| `returns_refunds` | 88 | 0.7692 | 0.7955 | 0.7821 |
| `shipping_policy` | 50 | 0.9697 | 0.6400 | 0.7711 |

## 5. Intent Confusion Matrix

| Expected \ Predicted | `account_payment` | `order_status` | `other` | `other_invalid` | `returns_refunds` | `shipping_policy` |
|---|---|---|---|---|---|---|
| `account_payment` | 90 | 1 | 5 | 4 | 1 | 1 |
| `order_status` | 1 | 96 | 6 | 2 | 5 | 0 |
| `other` | 5 | 8 | 135 | 10 | 11 | 0 |
| `returns_refunds` | 2 | 7 | 6 | 3 | 70 | 0 |
| `shipping_policy` | 0 | 8 | 3 | 3 | 4 | 32 |

## 6. Robustness & Security Integrity

- **Decision Consistency across Groups**: `0.8400`
- **Intent Consistency vs Parent**: `0.7233`
- **Paraphrase Consistency**: `0.7500`
- **Prompt Injection Violations**: `0`
- **Priority Gaming Violations**: `0`
- **Conflicting Information Accuracy**: `0.4091`

## 7. Stochastic Tier-2 Reliability (k-pass)

| Field | Krippendorff's Alpha | Mean Pairwise Agreement | SD Cases | SD Passes | Status |
|---|---|---|---|---|---|
| `decision` | 0.8451 | 0.9414 | 0.0946 | 0.0168 | ✅ Reliable |
| `escalate` | 0.8451 | 0.9414 | 0.4126 | 0.0220 | ✅ Reliable |
| `intent` | 0.9568 | 0.9744 | 0.0643 | 0.0127 | ✅ Reliable |

Unstable cases count: `23`

## 8. Top Failure Clusters

### Error Type Summary

| Error Type | Deciding Tier | Count | Share |
|---|---|---|---|
| `false_escalation` | `tier1_router_s1` | 85 | 38.46% |
| `missed_escalation` | `tier2_flash` | 71 | 32.13% |
| `false_escalation` | `tier2_flash` | 22 | 9.95% |
| `missed_escalation` | `tier1_router_s1` | 15 | 6.79% |
| `wrong_intent` | `tier2_flash` | 10 | 4.52% |
| `wrong_intent` | `tier1_router_s1` | 9 | 4.07% |
| `invalid_label` | `tier0_rules` | 5 | 2.26% |
| `wrong_queue` | `tier1_router_s1` | 4 | 1.81% |

### Top Failure Clusters

| Rank | Error Type | Transition | Deciding Tier | Size | Severity | Score | Signature Terms |
|---|---|---|---|---|---|---|---|
| 1 | `missed_escalation` | `other->other` | `tier2_flash` | 67 | 5 | 335 | `user, url, help, amazon, app` |
| 2 | `false_escalation` | `order_status->order_status` | `tier1_router_s1` | 31 | 2 | 62 | `user, order, delivery, url, still` |
| 3 | `false_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 18 | 2 | 36 | `bloody, payment, help, need, user` |
| 4 | `false_escalation` | `returns_refunds->returns_refunds` | `tier1_router_s1` | 16 | 2 | 32 | `return, user, order, one, need` |
| 5 | `missed_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 4 | 5 | 20 | `amazon, wire, card, user, international` |
| 6 | `missed_escalation` | `other->order_status` | `tier1_router_s1` | 4 | 5 | 20 | `user, received, ce, amazon, home` |
| 7 | `invalid_label` | `*->human_escalation` | `tier0_rules` | 5 | 3 | 15 | `fucking, address, delivery, change, wanna` |
| 8 | `missed_escalation` | `other->returns_refunds` | `tier1_router_s1` | 3 | 5 | 15 | `amazon, user, need, url, being` |
| 9 | `missed_escalation` | `returns_refunds->returns_refunds` | `tier1_router_s1` | 3 | 5 | 15 | `url, search, wrong, user, box` |
| 10 | `false_escalation` | `order_status->order_status` | `tier2_flash` | 6 | 2 | 12 | `user, guys, url, et, deliver` |

### Representative Failure Tickets

#### Cluster 1 (`missed_escalation` — `other->other`)
- **rt-kaggle_twcs-2bc59797**: "Need Your Help... @user" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)
- **rt-synthetic_template-50b2c47a**: "help me please" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)

#### Cluster 2 (`false_escalation` — `order_status->order_status`)
- **rt-synthetic_jetski-250e3eb9**: "@user Order [ORDER] past delivery date, tracking has no updates and seller not answering. How do I check where the shipment is?" (Expected: `auto_rag/order_status`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-synthetic_jetski-0342b66a**: "Hey @user, hoping you can help with order [ORDER]. The estimated arrival date was last week and it still hasn't shown up, plus the tracking info never updated. I tried messaging the seller with no res" (Expected: `auto_rag/order_status`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

#### Cluster 3 (`false_escalation` — `account_payment->account_payment`)
- **rt-bitext_cs-2533236e**: "I need help opening a new goddamn gold account" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)
- **rt-bitext_cs-d05785be**: "help me opening a new goddamn standard account acconut" (Expected: `auto_rag/account_payment`, Predicted: `escalate/account_payment`, Tier: `tier1_router_s1`)

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

- **Total Run Cost**: `$0.2842`
- **Decision Cost**: `$0.2604`
- **Reliability Cost (k-pass)**: `$0.0238`
- **Cost per 1,000 Tickets**: `$0.5017`
- **Projected Monthly Cost (1M tickets)**: `$501.73`
- **Projected Monthly Cost (10M tickets)**: `$5017.29`
- **Policy Latency p50**: `0.004s` | **p95**: `0.008s` | **p99**: `0.011s` | **Max**: `0.013s`

### Per-Tier Latency Breakdown

| Tier | Calls | p50 Latency | p95 Latency | p99 Latency | Max Latency |
|---|---|---|---|---|---|
| `tier1_router_s1` | 497 | n/a* | n/a* | n/a* | n/a* |
| `tier2_flash` | 546 | n/a* | n/a* | n/a* | n/a* |

*Note: Tiers marked with `n/a*` lack measured live wall-clock latency (cached stubs or replay entries). Run with `--refresh-cache` against live endpoints for measured latency.*

## 10. Per-Slice Performance

### By Source Dataset

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `bitext_cs` | 80 | 0.6000 | 0.6375 | 0.8250 |
| `kaggle_twcs` | 280 | 0.5643 | 0.6036 | 0.8036 |
| `perturbation` | 17 | 0.4118 | 0.4706 | 0.6471 |
| `synthetic_jetski` | 112 | 0.5982 | 0.6339 | 0.8661 |
| `synthetic_template` | 30 | 0.6000 | 0.7333 | 0.8000 |

### By Scenario

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `ambiguous` | 22 | 0.8636 | 0.8636 | 0.9091 |
| `conflicting` | 22 | 0.4091 | 0.5909 | 0.5455 |
| `long` | 20 | 0.7000 | 0.7500 | 0.9500 |
| `missing_info` | 22 | 0.7273 | 0.7273 | 1.0000 |
| `noisy` | 19 | 0.6842 | 0.6842 | 0.9474 |
| `normal` | 342 | 0.5848 | 0.6257 | 0.7982 |
| `ood` | 66 | 0.3636 | 0.4242 | 0.7727 |
| `paraphrase` | 12 | 0.5000 | 0.5833 | 0.9167 |
| `perturbation` | 17 | 0.4118 | 0.4706 | 0.6471 |
| `prompt_injection` | 13 | 0.3077 | 0.3077 | 0.6923 |
| `rare` | 19 | 0.6316 | 0.6842 | 0.7895 |

### By Deciding Tier

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `tier0_rules` | 22 | 0.7727 | 0.7727 | 0.0000 |
| `tier1_router_s1` | 315 | 0.6413 | 0.6825 | 0.8349 |
| `tier2_flash` | 182 | 0.4341 | 0.4890 | 0.8791 |

### By Label Status

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `disputed` | 120 | 0.4250 | 0.5000 | 0.7667 |
| `provisional` | 309 | 0.6117 | 0.6408 | 0.8447 |
| `verified` | 90 | 0.6444 | 0.7000 | 0.7778 |

### By Laya Contamination (in-distribution collision)

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `clean` | 389 | 0.5733 | 0.6170 | 0.8201 |
| `contaminated` | 130 | 0.5769 | 0.6231 | 0.8000 |

## 11. Degraded / Error Cases

Zero degraded or error cases encountered.

## 12. Baseline vs Candidate Comparison

### Verdict: ❌ `REJECT_BLOCKED_PREEXISTING`

- Candidate failed hard gates.
- Gate NOT_EVALUATED: strict_safety_recall - Strict safety recall not evaluated: no verified strict safety cases (n=0)
- Gate NOT_EVALUATED: regression_pass_rate - run regression split
- Gate PRE_EXISTING: taxonomy_validity - Taxonomy validity rate 0.9576 vs min 1.0000 (invalid: human_escalation 22 tier0_rules)
- Improves the objective (delta=0.1247, 95% CI=[0.0802, 0.1769]) and introduces no new gate failures; blocked only by gates that also fail or are not evaluated in the baseline.

| Metric Delta | Point Estimate | 95% Paired-Bootstrap CI |
|---|---|---|
| `decision_accuracy` | n/a | [-0.0173, 0.0116] |
| `intent_accuracy` | n/a | [0.1448, 0.2121] |
| `objective` | 0.1247 | [0.0802, 0.1769] |
| `routing_correctness` | 0.0983 | [0.0739, 0.1291] |

- **McNemar Routing-Correctness p-value**: `0.000000`
- **McNemar Decision-Correctness p-value**: `1.000000`
- **Wins**: 53 | **Losses**: 2 | **Ties**: 464
- **Newly Failing Regression Cases**: `0`

