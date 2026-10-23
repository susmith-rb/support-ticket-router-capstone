# Specification: System-1 Router (`router-s1`)

This specification defines the typed-decision taxonomy, PII redaction rules, teacher-labeling protocol, dataset stratification, model evaluation metrics, threshold sweeping, and routing invariants for the Tier-1 router (`router-s1`) based on the fine-tuned Laya encoder.

---

## 1. Overview & Tiered Architecture

The routing pipeline implements System-1 / System-2 tiered triage (TDD §5) to achieve sub-100 ms routing while ensuring angry and urgent customer inquiries bypass automation:

```
Inbound Support Message
         │
         ▼
[Tier 0: Model Armor + Deterministic Rules] ──▶ Hard escalations (threats/fraud), shouting, non-English
         │                                       (PII redacted via Luhn-verified rules)
         ▼
[Tier 1: Fine-Tuned Laya Encoder (ModernBERT)] ──▶ p_esc = max(P(escalate), P(angry))
         │                                         Calibration ECE ≤ 0.08, latency p50 43 ms
         ├── p_esc ≥ τ_esc (0.50) ───────────────▶ Human Urgent Queue (immediate acknowledgement)
         ├── p_esc < τ_low (0.15) ───────────────▶ Auto-Resolution: FaqResolverAgent / Agent Search
         └── τ_low ≤ p_esc < τ_esc ──────────────▶ Tier 2: Gemini 3.8 Flash (Uncertainty Band)
```

### Invariant: Add-Only Escalation

Per AGENTS.md Non-negotiable 1 and TDD §5.1:
$$\text{final.escalate} = \text{rules} \lor \text{tier1} \lor \text{tier2}$$
No downstream tier or rule may clear or revoke an escalation decision flagged by an earlier tier.

---

## 2. Decision Taxonomy

The router evaluates four discrete decision dimensions over each customer inquiry:

| Dimension | Type | Choices | Description |
|---|---|---|---|
| `escalate` | Binary | `escalate`, `do_not_escalate` | Priority flag indicating whether a human specialist is required. |
| `sentiment` | Multi-class | `angry`, `neutral`, `positive` | Emotional valence of the customer message. |
| `urgency` | Ordinal | `immediate`, `soon`, `normal` | Time-sensitivity of the inquiry. |
| `intent` | Multi-class | `account_payment`, `order_status`, `returns_refunds`, `shipping_policy`, `other` | Core functional domain of the inquiry. |

---

## 3. Measured Metrics Summary

All numbers below are extracted directly from `training/router_s1` (verified runs on Workbench VM `laya-tune` with 1×T4 GPU, documented in `training/router_s1/README.md`):

### Model Evolution Across Checkpoints

| Checkpoint | Dataset Split | Sentiment Acc / F1 | Intent Acc / F1 | Urgency Exact / Within-1 | In-Process Latency (T4) | Notes |
|---|---|---|---|---|---|---|
| **Zero-Shot Baseline** | v1 test (199 msgs) | 0.582 / 0.490 | 0.599 / 0.529 | 0.19 / 0.53 | p50 39 ms | Untrained `convaiinnovations/laya` base checkpoint (training/router_s1/README.md line 106). |
| **router-s1 v1** | 2,000 tweets (1602 / 199 / 199) | 0.754 / 0.754 | 0.814 / 0.750 | 0.69 / 0.980 | p50 39 ms, p95 46 ms | Soft-CE loss, 4 epochs (376 updates), 21 min on T4. Intent ECE 0.053 (training/router_s1/README.md line 105). |
| **router-s1 v2** | 6,446 tweets (5154 / 646 / 646) | **0.802 / 0.780** | **0.858 / 0.819** | **0.714 / 0.985** | **p50 42.9 ms, p95 51.2 ms** | Soft-CE loss, 3 epochs (948 updates), 52 min on T4. Intent ECE 0.053, Sentiment ECE 0.086 (training/router_s1/README.md line 107). |

### Strict DoD Use-Case Simulation (v2 Test Set, n=646, 122 Strict Positives)

Strict "must reach a human" set = gold sentiment `angry` OR urgency `immediate` (18.9% of traffic, training/router_s1/README.md line 113):

| Ruleset Configuration | τ_low | Test Recall (Bound) | Human Queue Share (Tier 0 + Tier 1 ≥ 0.5) | Tier 2 Share (Flash) | Auto Share |
|---|---|---|---|---|---|
| **None (τ_low = 0.15)** | 0.15 | 0.967 | 34.7% | **12.4%** | 52.9% |
| **None (τ_low = 0.05)** | 0.05 | 0.975 | 34.7% | 22.1% | 43.2% |
| **Lean Rules** (shouting, negative lexicon, repeat) | 0.05 | 0.984 | 34.7% | 25.2% | 40.1% |
| **Full Rules** (+ deadline, non-English → Tier 2) | 0.05 | 0.992 | 34.7% | 38.9% | 26.5% |

- At `τ_low = 0.15`, Tier 2 routing volume matches the TDD's 12% architectural estimate while auto-deflecting 52.9% of traffic (training/router_s1/README.md line 117).
- Broad `escalate` label (36% of tweets) reflects general priority, whereas strict angry/urgent (19%) governs the human bypass guarantee (training/router_s1/README.md line 124).

---

## 4. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-ROUTER-S1-001` | Four-Question Router Decision Taxonomy | `done` | `tests/training/test_router_schema.py::test_schema_required_questions` |
| `REQ-ROUTER-S1-002` | Multi-Stage PII Redaction with Luhn Verification | `done` | `tests/training/test_redact.py::test_redact` |
| `REQ-ROUTER-S1-003` | Teacher Labeling and Soft Vote-Share Gold Generation | `done` | `tests/training/test_label_teacher.py::test_to_gold_unanimous_votes` |
| `REQ-ROUTER-S1-004` | Stratified Dataset Construction with Hard Labels | `done` | `tests/training/test_build_dataset.py::test_argmax_basic` |
| `REQ-ROUTER-S1-005` | Multi-Class Calibration and Metric Evaluation | `done` | `tests/training/test_evaluate.py::test_ece_perfect_calibration` |
| `REQ-ROUTER-S1-006` | Threshold Sweeping for Escalation and Uncertainty Band | `done` | `tests/training/test_evaluate.py::test_sweep` |
| `REQ-ROUTER-S1-007` | Golden Set Inter-Annotator Agreement Scoring | `done` | `tests/training/test_golden_score.py::test_kappa_perfect_agreement` |
| `REQ-ROUTER-S1-008` | Tier 0 Deterministic Escalation Rules | `done` | `tests/training/test_simulate_system.py::test_tier0` |
| `REQ-ROUTER-S1-009` | Multi-Tier Routing Simulation and Routing Invariant | `done` | `tests/training/test_simulate_system.py::test_run_routing` |
| `REQ-ROUTER-S1-010` | Strict DoD Angry/Urgent Routing Recall Simulation | `done` | `tests/training/test_strict_recall.py::test_strict_run` |
| `REQ-ROUTER-S1-011` | Human-Verified Golden Set Promotion | `partial` | `manual: python training/router_s1/pipeline/golden_score.py a.csv b.csv golden_key.jsonl` |
| `REQ-ROUTER-S1-012` | Target System Recall ≥ 0.98 for Angry/Urgent Inquiries | `partial` | `manual: python training/router_s1/pipeline/strict_recall.py` |
| `REQ-ROUTER-S1-013` | Vertex AI Model Registry and Endpoint Serving | `open` | `manual: gcloud ai models upload --region=us-central1 --display-name=router-s1-v2` |

---

## 5. Detailed Requirements

### REQ-ROUTER-S1-001: Four-Question Router Decision Taxonomy
- **Statement**: The Tier-1 schema MUST define exactly four typed-decision questions (`escalate`, `sentiment`, `urgency`, `intent`) with fixed, mutually exclusive choices and calibrated output probabilities.
- **Rationale**: Implements the System-1 typed-decision contract required by the Laya encoder (TDD §5.1, §5.2).
- **Acceptance**: `tests/training/test_router_schema.py::test_schema_required_questions`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-002: Multi-Stage PII Redaction with Luhn Verification
- **Statement**: Prior to model ingestion or persistence, text MUST be redacted of customer identifiers (order IDs, tracking numbers, phone numbers, email addresses, credit cards validated by the Luhn algorithm, user handles), running order-ID extraction before card matching.
- **Rationale**: Prevents Amazon order IDs (3-7-7 digits) from falsely matching credit card patterns and eliminates PII leakage into models and logs (TDD §8.1, §8.2; AGENTS.md rule 3; docs/LESSONS.md L6).
- **Acceptance**: `tests/training/test_redact.py::test_redact`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-003: Teacher Labeling and Soft Vote-Share Gold Generation
- **Statement**: The teacher pipeline MUST query `gemini-3.1-pro-preview` with 3-sample self-consistency voting, computing soft target distributions across question choices for all messages with ≥ 2 successful votes.
- **Rationale**: Soft vote shares capture natural language label ambiguity and improve probability calibration during distillation (TDD §5.2; docs/LESSONS.md L11; training/router_s1/README.md lines 54–58).
- **Acceptance**: `tests/training/test_label_teacher.py::test_to_gold_unanimous_votes`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-004: Stratified Dataset Construction with Hard Labels
- **Statement**: The dataset builder MUST stratify splits across intent classes (80% train, 10% val, 10% test) and compute hard evaluation labels via deterministic argmax with stable lexicographical tie-breaking.
- **Rationale**: Prevents train/test skew across minority intents and guarantees reproducible test sets (TDD §5.2).
- **Acceptance**: `tests/training/test_build_dataset.py::test_argmax_basic`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-005: Multi-Class Calibration and Metric Evaluation
- **Statement**: Evaluation routines MUST compute argmax accuracy, macro-averaged F1, and 15-bin Expected Calibration Error (ECE) for multi-class decisions, alongside exact and within-one accuracy for ordinal urgency.
- **Rationale**: Ensures predicted class probabilities accurately reflect true empirical risk before thresholding (TDD §5.2, §12.3).
- **Acceptance**: `tests/training/test_evaluate.py::test_ece_perfect_calibration`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-006: Threshold Sweeping for Escalation and Uncertainty Band
- **Statement**: The evaluation suite MUST sweep candidate escalation thresholds τ ∈ [0.05, 0.95] across validation sets, reporting recall, false-escalation rate, and the largest threshold maintaining recall 1.0 (`tau_esc_max_recall1`).
- **Rationale**: Informs selection of operating thresholds τ_low and τ_esc based on empirical validation distributions (TDD §5.2).
- **Acceptance**: `tests/training/test_evaluate.py::test_sweep`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-007: Golden Set Inter-Annotator Agreement Scoring
- **Statement**: The golden scoring tool MUST calculate pairwise Cohen's Kappa (κ) and agreement matrices across independent human or cross-family model annotators.
- **Rationale**: Establishes ground-truth label reliability (target κ ≥ 0.80) for the reference evaluation benchmark (TDD §5.2, §12.3; training/router_s1/README.md lines 83–86).
- **Acceptance**: `tests/training/test_golden_score.py::test_kappa_perfect_agreement`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-008: Tier 0 Deterministic Escalation Rules
- **Statement**: Tier 0 deterministic screening MUST evaluate hard safety triggers (fraud, security, legal threats), soft escalation cues (negative sentiment lexicon), all-caps shouting detection, and non-English language heuristics.
- **Rationale**: Acts as a synchronous zero-latency safety filter catching high-risk inquiries before model execution (TDD §5.1, §5.2).
- **Acceptance**: `tests/training/test_simulate_system.py::test_tier0`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-009: Multi-Tier Routing Simulation and Routing Invariant
- **Statement**: The tiered simulation pipeline MUST evaluate tickets sequentially through Tier 0, Tier 1, and Tier 2, enforcing `p_esc = max(P(escalate), P(angry))` and guaranteeing that escalation flags are add-only.
- **Rationale**: Protects customer safety by ensuring an escalation triggered by any tier cannot be dismissed (TDD §5.1; AGENTS.md rule 1).
- **Acceptance**: `tests/training/test_simulate_system.py::test_run_routing`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-010: Strict DoD Angry/Urgent Routing Recall Simulation
- **Statement**: System routing simulations MUST measure recall against the strict business DoD definition (`sentiment="angry"` OR `urgency="immediate"`) across varying rule configurations.
- **Rationale**: Aligns evaluation directly with the capstone business goal of prioritizing frustrated customers (TDD §2.3 D2; docs/LESSONS.md L14; training/router_s1/README.md lines 109–125).
- **Acceptance**: `tests/training/test_strict_recall.py::test_strict_run`
- **Status**: done
- **Owner**: router-s1

### REQ-ROUTER-S1-011: Human-Verified Golden Set Promotion
- **Statement**: A 300-case stratified test dataset SHOULD be independently reviewed by two human annotators to establish a verified answer key (`data/golden/v1.jsonl`) for production threshold gating.
- **Rationale**: Replaces teacher model self-consistency bounds with verified ground truth (TDD §5.2, §12.3; training/router_s1/README.md lines 83–86).
- **Acceptance**: `manual: python training/router_s1/pipeline/golden_score.py a.csv b.csv golden_key.jsonl`
- **Status**: partial
- **Owner**: router-s1

### REQ-ROUTER-S1-012: Target System Recall ≥ 0.98 for Angry/Urgent Inquiries
- **Statement**: The complete tiered routing system SHOULD achieve system recall ≥ 0.98 on the human-verified golden set for angry or urgent inquiries, reporting the 95% lower confidence bound.
- **Rationale**: Core AI/ML performance objective driving the customer satisfaction North Star metric (TDD §1.1, §2.3 D2).
- **Acceptance**: `manual: python training/router_s1/pipeline/strict_recall.py`
- **Status**: partial
- **Owner**: router-s1

### REQ-ROUTER-S1-013: Vertex AI Model Registry and Endpoint Serving
- **Statement**: The fine-tuned `router-s1` v2 checkpoint SHOULD be uploaded to Vertex AI Model Registry and deployed to a dedicated Vertex AI Prediction endpoint on a g2-standard-4 (1×L4) GPU accepting `:rawPredict` calls.
- **Rationale**: Delivers scalable, low-latency (<100 ms p95) serving without JSON key reordering (TDD §4.1, §5.4; AGENTS.md rule 4; training/router_s1/README.md lines 88–96).
- **Acceptance**: `manual: gcloud ai models upload --region=us-central1 --display-name=router-s1-v2`
- **Status**: open
- **Owner**: router-s1

---

## 6. Open Questions / Deltas

1. **100% Recall vs. System Recall Target**: Scoping criterion D1 specifies that 100% of tickets *classified* as angry or urgent must reach human queues (the add-only routing invariant). The classification-recall target on the golden set is defined as `recall ≥ 0.98` with a 95% lower bound (TDD §2.3 D2). Current Tier 0+1 simulation achieves `0.967` at 12.4% Tier-2 share, and `0.984–0.992` with fuller rules (training/router_s1/README.md lines 117–120). The persistent misses are sarcasm, single-word all-caps, and non-English inputs.
2. **Human Golden Set Completion**: The 300-case golden set sheet (`training/router_s1/golden/golden_set_labeling.xlsx`) contains cross-family labels from Gemini 3.1 Pro, Claude Sonnet 5.5, and Claude Opus 5.5, plus a reduced 146-case review tab. Completion of human dual-labeling is pending.
3. **Vertex Serving Deployment**: Model artifacts reside in Cloud Storage (`gs://susmith-fde-ticket-router-laya/checkpoints/router-s1/v2/`). Deployment to an active Vertex AI Prediction endpoint is pending GPU quota approval in Argolis (TDD §4.1; training/router_s1/README.md lines 88–96).
4. **Multilingual Inquiries**: The fine-tuned ModernBERT base checkpoint is English-only. Non-English tweets currently trigger a Tier-0 heuristic directing them to Tier 2. Extending the base to a multilingual checkpoint is deferred to Phase 2 (TDD §18.3).
