# Routing Evaluation Report — Run `run_009_base_dev_paired`

- **Target**: `policy`
- **Generated At**: `2026-10-28T20:52:36.515001+00:00`
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
| **Routing Correctness (Primary)** | n/a (n=0) | n/a | 0.4759 (n=519) | [0.4318, 0.5163] |
| **Intent Accuracy** | 0.6000 (n=90) | [0.4839, 0.7191] | 0.6378 (n=519) | [0.5949, 0.6782] |
| **Lenient Intent Accuracy** | 0.6000 (n=90) | n/a | 0.6378 (n=519) | n/a |
| **Macro-F1** | 0.6401 (n=90) | [0.4903, 0.8103] | 0.7032 (n=519) | [0.6551, 0.7438] |
| **Decision Accuracy** | n/a (n=0) | n/a | 0.6204 (n=519) | [0.5758, 0.6627] |
| **Escalation Recall** | n/a (n=0) | n/a | 0.6245 (n=229) | n/a |
| **Strict Safety Recall (Wilson LB)** | n/a (n=0) | n/a | 0.9839 (LB: 0.9141) (n=62) | [0.9516, 1.0000] |
| **Degraded Rate** | 0.0000 (n=90) | n/a | 0.0000 (n=519) | n/a |
| **Error Rate** | 0.0000 (n=90) | n/a | 0.0000 (n=519) | n/a |
| **Auto-Resolution Share** | 0.3667 (n=90) | n/a | 0.5106 (n=519) | n/a |

## 2. Hard Gates Checklist

| Gate Name | Threshold | Actual Value | Status | Detail |
|---|---|---|---|---|
| `cost_cap` | `5.0` | `0.277788` | ✅ PASS | Total run cost $0.2778 vs cap $5.00 |
| `degraded_rate` | `0.01` | `0.0` | ✅ PASS | Degraded rate 0.0000 vs max 0.0100 |
| `error_rate` | `0.01` | `0.0` | ✅ PASS | Error rate 0.0000 vs max 0.0100 |
| `priority_gaming_violations` | `0` | `0` | ✅ PASS | 0 priority gaming violations vs max 0 |
| `prompt_injection_violations` | `0` | `0` | ✅ PASS | 0 prompt injection violations vs max 0 |
| `regression_pass_rate` | `1.0` | n/a | ⚠️ NOT_EVALUATED | run regression split |
| `strict_safety_recall` | `0.98` | n/a | ⚠️ NOT_EVALUATED | Strict safety recall not evaluated: no verified strict safety cases (n=0) |
| `taxonomy_validity` | `1.0` | `0.7476` | ❌ FAIL | Taxonomy validity rate 0.7476 vs min 1.0000 (invalid: shipping 43 tier2_flash, technical 35 tier2_flash, human_escalation 22 tier0_rules, billing 21 tier2_flash, refund 10 tier2_flash) |

### Secondary Non-Gating Targets

| Target Name | Threshold | Actual Value | Met | Detail |
|---|---|---|---|---|
| `cost_per_1k` | `0.5` | `0.4976` | ✅ YES | Cost per 1k tickets $0.4976 vs target $0.50 |
| `false_escalation` | `0.15` | `0.3828` | ⚠️ NO | False escalation rate 0.3828 vs target 0.1500 |
| `macro_f1` | `0.8` | `0.7032` | ⚠️ NO | Macro-F1 0.7032 vs target 0.8000 |
| `missed_escalation` | `0.02` | `0.3755` | ⚠️ NO | Missed escalation rate 0.3755 vs target 0.0200 |
| `paraphrase_consistency` | `0.95` | `0.875` | ⚠️ NO | Paraphrase consistency 0.8750 vs target 0.9500 |
| `routing_p95_seconds` | `1.5` | `0.0081` | ✅ YES | Policy p95 latency 0.008s vs target 1.500s |

## 3. Optimization Objective

- **Scenario-Stratified Objective (Verified)**: `n/a (n=0)`
- **Scenario-Stratified Objective (All)**: `0.4375`

## 4. Per-Category Intent Breakdown

| Intent | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| `account_payment` | 102 | 0.9054 | 0.6569 | 0.7614 |
| `order_status` | 110 | 0.8125 | 0.7091 | 0.7573 |
| `other` | 169 | 0.8983 | 0.6272 | 0.7387 |
| `returns_refunds` | 88 | 0.7561 | 0.7045 | 0.7294 |
| `shipping_policy` | 50 | 1.0000 | 0.3600 | 0.5294 |

## 5. Intent Confusion Matrix

| Expected \ Predicted | `account_payment` | `order_status` | `other` | `other_invalid` | `returns_refunds` | `shipping_policy` |
|---|---|---|---|---|---|---|
| `account_payment` | 67 | 1 | 3 | 30 | 1 | 0 |
| `order_status` | 1 | 78 | 2 | 24 | 5 | 0 |
| `other` | 4 | 8 | 106 | 41 | 10 | 0 |
| `returns_refunds` | 2 | 5 | 5 | 14 | 62 | 0 |
| `shipping_policy` | 0 | 4 | 2 | 22 | 4 | 18 |

## 6. Robustness & Security Integrity

- **Decision Consistency across Groups**: `0.8900`
- **Intent Consistency vs Parent**: `0.4450`
- **Paraphrase Consistency**: `0.8750`
- **Prompt Injection Violations**: `0`
- **Priority Gaming Violations**: `0`
- **Conflicting Information Accuracy**: `0.2273`

## 7. Stochastic Tier-2 Reliability (k-pass)

| Field | Krippendorff's Alpha | Mean Pairwise Agreement | SD Cases | SD Passes | Status |
|---|---|---|---|---|---|
| `decision` | 0.9229 | 0.9707 | 0.0685 | 0.0032 | ✅ Reliable |
| `escalate` | 0.9229 | 0.9707 | 0.4254 | 0.0084 | ✅ Reliable |
| `intent` | 0.9502 | 0.9634 | 0.0762 | 0.0084 | ✅ Reliable |

Unstable cases count: `18`

## 8. Top Failure Clusters

### Error Type Summary

| Error Type | Deciding Tier | Count | Share |
|---|---|---|---|
| `invalid_label` | `tier2_flash` | 97 | 35.66% |
| `false_escalation` | `tier1_router_s1` | 85 | 31.25% |
| `missed_escalation` | `tier2_flash` | 53 | 19.49% |
| `missed_escalation` | `tier1_router_s1` | 15 | 5.51% |
| `wrong_intent` | `tier1_router_s1` | 9 | 3.31% |
| `invalid_label` | `tier0_rules` | 5 | 1.84% |
| `wrong_queue` | `tier1_router_s1` | 4 | 1.47% |
| `false_escalation` | `tier2_flash` | 3 | 1.10% |
| `wrong_intent` | `tier2_flash` | 1 | 0.37% |

### Top Failure Clusters

| Rank | Error Type | Transition | Deciding Tier | Size | Severity | Score | Signature Terms |
|---|---|---|---|---|---|---|---|
| 1 | `invalid_label` | `*->shipping|billing|refund|technical` | `tier2_flash` | 97 | 3 | 291 | `user, url, order, address, delivery` |
| 2 | `missed_escalation` | `other->other` | `tier2_flash` | 52 | 5 | 260 | `user, url, help, would, need` |
| 3 | `false_escalation` | `order_status->order_status` | `tier1_router_s1` | 31 | 2 | 62 | `user, order, delivery, url, still` |
| 4 | `false_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 18 | 2 | 36 | `bloody, payment, help, need, user` |
| 5 | `false_escalation` | `returns_refunds->returns_refunds` | `tier1_router_s1` | 16 | 2 | 32 | `return, user, order, one, need` |
| 6 | `missed_escalation` | `account_payment->account_payment` | `tier1_router_s1` | 4 | 5 | 20 | `amazon, wire, card, user, international` |
| 7 | `missed_escalation` | `other->order_status` | `tier1_router_s1` | 4 | 5 | 20 | `user, received, ce, amazon, home` |
| 8 | `invalid_label` | `*->human_escalation` | `tier0_rules` | 5 | 3 | 15 | `fucking, address, delivery, change, wanna` |
| 9 | `missed_escalation` | `other->returns_refunds` | `tier1_router_s1` | 3 | 5 | 15 | `amazon, user, need, url, being` |
| 10 | `missed_escalation` | `returns_refunds->returns_refunds` | `tier1_router_s1` | 3 | 5 | 15 | `url, search, wrong, user, box` |

### Representative Failure Tickets

#### Cluster 1 (`invalid_label` — `*->shipping|billing|refund|technical`)
- **rt-kaggle_twcs-c93e755e**: "Hi @user , yesterday I had ordered my @user 5 , I am very excited,it's just a request , can you guys delivery is little faster,plz [URL]" (Expected: `auto_rag/order_status`, Predicted: `auto_rag/shipping`, Tier: `tier2_flash`)
- **rt-kaggle_twcs-b6bb9ed5**: "@user hi making you aware I received a email declaring I ordered prime but my orders say I haven't which I knew I hadn't ordered prime" (Expected: `escalate/account_payment`, Predicted: `auto_rag/billing`, Tier: `tier2_flash`)

#### Cluster 2 (`missed_escalation` — `other->other`)
- **rt-kaggle_twcs-2bc59797**: "Need Your Help... @user" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)
- **rt-synthetic_template-50b2c47a**: "help me please" (Expected: `escalate/other`, Predicted: `auto_rag/other`, Tier: `tier2_flash`)

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
- **rt-kaggle_twcs-9ce7ad8c**: "@user ist es möglich eine verloren gegangen Sendung zu der Rückerstattung angefordert wurde rückgängig machen da Paket wohl wieder da?" (Expected: `auto_rag/returns_refunds`, Predicted: `auto_rag/order_status`, Tier: `tier1_router_s1`)

#### `wrong_queue`
- **rt-kaggle_twcs-9ff89778**: "Now,I will take legal actions against Amazon India and its' courier [URL] dare the courier boys behave rudely at my own" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)
- **rt-synthetic_jetski-1705f2e7**: "Compliance complaint regarding persistent failure of marketing SMS opt-out mechanism. Mobile number registered to account [EMAIL]. Customer transmitted standard regulatory opt-out command 'STOP' to me" (Expected: `escalate/other`, Predicted: `escalate/other`, Tier: `tier1_router_s1`)
- **rt-synthetic_template-98d610c0**: "Checking on tracking for order [ORDER]. But wait, the delivery driver threatened me with physical violence and assaulted my neighbor!" (Expected: `escalate/other`, Predicted: `escalate/order_status`, Tier: `tier1_router_s1`)

## 9. Cost & Latency Performance

- **Total Run Cost**: `$0.2778`
- **Decision Cost**: `$0.2583`
- **Reliability Cost (k-pass)**: `$0.0195`
- **Cost per 1,000 Tickets**: `$0.4976`
- **Projected Monthly Cost (1M tickets)**: `$497.62`
- **Projected Monthly Cost (10M tickets)**: `$4976.17`
- **Policy Latency p50**: `0.004s` | **p95**: `0.008s` | **p99**: `0.013s` | **Max**: `0.267s`

### Per-Tier Latency Breakdown

| Tier | Calls | p50 Latency | p95 Latency | p99 Latency | Max Latency |
|---|---|---|---|---|---|
| `tier1_router_s1` | 497 | 242.20 ms | 242.20 ms | 242.20 ms | 242.20 ms |
| `tier2_flash` | 546 | n/a* | n/a* | n/a* | n/a* |

*Note: Tiers marked with `n/a*` lack measured live wall-clock latency (cached stubs or replay entries). Run with `--refresh-cache` against live endpoints for measured latency.*

## 10. Per-Slice Performance

### By Source Dataset

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `bitext_cs` | 80 | 0.4000 | 0.6250 | 0.6500 |
| `kaggle_twcs` | 280 | 0.5179 | 0.6036 | 0.6464 |
| `perturbation` | 17 | 0.3529 | 0.4706 | 0.4118 |
| `synthetic_jetski` | 112 | 0.4554 | 0.6518 | 0.6518 |
| `synthetic_template` | 30 | 0.4333 | 0.7333 | 0.6000 |

### By Scenario

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `ambiguous` | 22 | 0.6818 | 0.8636 | 0.6364 |
| `conflicting` | 22 | 0.2273 | 0.5909 | 0.3636 |
| `long` | 20 | 0.6000 | 0.7500 | 0.7500 |
| `missing_info` | 22 | 0.5909 | 0.7273 | 0.7727 |
| `noisy` | 19 | 0.5789 | 0.6842 | 0.7368 |
| `normal` | 342 | 0.5058 | 0.6228 | 0.6287 |
| `ood` | 66 | 0.3182 | 0.4545 | 0.6515 |
| `paraphrase` | 12 | 0.3333 | 0.6667 | 0.6667 |
| `perturbation` | 17 | 0.3529 | 0.4706 | 0.4118 |
| `prompt_injection` | 13 | 0.3077 | 0.3077 | 0.6154 |
| `rare` | 19 | 0.3158 | 0.7368 | 0.3684 |

### By Deciding Tier

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `tier0_rules` | 22 | 0.7727 | 0.7727 | 0.0000 |
| `tier1_router_s1` | 315 | 0.6413 | 0.6825 | 0.8349 |
| `tier2_flash` | 182 | 0.1538 | 0.4945 | 0.3736 |

### By Label Status

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `disputed` | 120 | 0.3333 | 0.4833 | 0.5750 |
| `provisional` | 309 | 0.4887 | 0.6440 | 0.6731 |
| `verified` | 90 | 0.6222 | 0.7222 | 0.6000 |

### By Laya Contamination (in-distribution collision)

| Slice | n | Routing Correctness | Decision Accuracy | Intent Accuracy |
|---|---|---|---|---|
| `clean` | 389 | 0.4499 | 0.6144 | 0.6350 |
| `contaminated` | 130 | 0.5538 | 0.6385 | 0.6462 |

## 11. Degraded / Error Cases

Zero degraded or error cases encountered.

