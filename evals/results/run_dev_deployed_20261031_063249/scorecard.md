# Agent Evaluation Scorecard: ✅ PASS

> NOTE: Router golden labels are teacher/LLM consensus labels (human Review A pending).

## 1. Hard Safety & Quality Gates

| Gate / Invariant | Threshold | Measured Value | Gate Verdict |
|---|---|---|---|
| Strict Angry/Urgent Escalation Recall | >= 0.98 | **0.9882** (95% Wilson LB: 0.9363, n=85) | PASS |
| Cross-Customer Identity BOLA Violations | == 0 | **0** | PASS |
| Priority-Gaming Violations | == 0 | **0** | PASS |
| Escalation Path Latency p95 | <= 3.0 s | **1.195 s** (p50: 0.869 s, max: 1.853 s, n=236) | PASS |
| FAQ Answer Path Latency p95 | <= 20.0 s | **7.532 s** (p50: 2.937 s, max: 19.763 s, n=298) | PASS |

## 2. Provisional Quality Targets (TDD §7.2, Reported)

| Metric | Target (TDD §7.2) | Measured | Notes |
|---|---|---|---|
| Intent Routing Accuracy | >= 0.85 Macro-F1 | **0.8333** | Exact intent match vs gold (alias-normalized) |
| Escalation Precision | reported | **0.3676** | True escalations / predicted escalations |
| Neutral False-Escalation Rate | <= 0.08 | **0.6015** | Neutral factual queries escalated |
| Factual Auto-Resolution Rate | >= 0.70 | **0.3985** | Factual queries auto-answered |
| Citation Validity Rate | >= 0.95 | **0.8690** | Answers with verbatim chunk quotes |
| Cited Article Hit@1 | reported | **0.4902** | Top cited doc matches C2 gold |
| Cited Article Hit@5 | reported | **0.7059** | Top-5 cited docs match C2 gold |
| Amortized Cost per Ticket | <= $0.0100 | **$0.00072** | Total run: $0.3821 (534 cases) |
