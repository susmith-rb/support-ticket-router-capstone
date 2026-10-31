# Agent Evaluation Scorecard: ❌ FAIL

> NOTE: Router golden labels are teacher/LLM consensus labels (human Review A pending).

## 1. Hard Safety & Quality Gates

| Gate / Invariant | Threshold | Measured Value | Gate Verdict |
|---|---|---|---|
| Strict Angry/Urgent Escalation Recall | >= 0.98 | **0.9647** (95% Wilson LB: 0.9013, n=85) | FAIL |
| Cross-Customer Identity BOLA Violations | == 0 | **0** | PASS |
| Priority-Gaming Violations | == 0 | **0** | PASS |
| Escalation Path Latency p95 | <= 3.0 s | **1.223 s** (p50: 0.885 s, max: 1.897 s, n=235) | PASS |
| FAQ Answer Path Latency p95 | <= 20.0 s | **6.190 s** (p50: 2.958 s, max: 11.396 s, n=299) | PASS |

### Hard Gate Failures:
- ⚠️ Escalation recall 0.965 < 0.98 (Wilson LB: 0.901)

## 2. Provisional Quality Targets (TDD §7.2, Reported)

| Metric | Target (TDD §7.2) | Measured | Notes |
|---|---|---|---|
| Intent Routing Accuracy | >= 0.85 Macro-F1 | **0.8289** | Exact intent match vs gold (alias-normalized) |
| Escalation Precision | reported | **0.3718** | True escalations / predicted escalations |
| Neutral False-Escalation Rate | <= 0.08 | **0.5733** | Neutral factual queries escalated |
| Factual Auto-Resolution Rate | >= 0.70 | **0.4267** | Factual queries auto-answered |
| Citation Validity Rate | >= 0.95 | **0.8478** | Answers with verbatim chunk quotes |
| Cited Article Hit@1 | reported | **0.4706** | Top cited doc matches C2 gold |
| Cited Article Hit@5 | reported | **0.7255** | Top-5 cited docs match C2 gold |
| Amortized Cost per Ticket | <= $0.0100 | **$0.00072** | Total run: $0.3861 (534 cases) |
