# Live Per-Path Latency Benchmark Report

- **Total Requests**: 160
- **Total Errors**: 0 (0.0%)
- **Overall Decision-Match Rate**: 91.2%

## Per-Path Performance Overview

| Path | N | Errors | p50 Total | p95 Total | p50 Turn | p50 Routing | p50 GenAnswer | Dominant Tier |
| :--- | -: | -: | -: | -: | -: | -: | -: | :--- |
| `greeting` | 20 | 0 | 1223.1 ms | 1496.0 ms | 350.2 ms | 0.2 ms | - | `tier0_rules` |
| `pleasantry` | 20 | 0 | 4174.2 ms | 25312.8 ms | 3026.7 ms | 1034.3 ms | 2128.6 ms | `tier0_rules` |
| `policy_faq` | 20 | 0 | 4658.4 ms | 21693.6 ms | 3765.0 ms | 151.4 ms | 2572.6 ms | `tier1_router_s1` |
| `order_list_auth` | 20 | 0 | 12508.5 ms | 42320.3 ms | 11564.8 ms | 4018.5 ms | 4906.2 ms | `tier2_flash` |
| `order_id_auth` | 20 | 0 | 7356.9 ms | 23367.4 ms | 6382.5 ms | 196.9 ms | 3369.7 ms | `tier1_router_s1` |
| `order_anon` | 20 | 0 | 1587.1 ms | 18187.3 ms | 623.0 ms | 188.8 ms | - | `tier1_router_s1` |
| `out_of_scope` | 20 | 0 | 4729.3 ms | 29657.8 ms | 3749.8 ms | 3379.5 ms | - | `tier2_flash` |
| `escalation` | 20 | 0 | 1243.9 ms | 6866.4 ms | 312.2 ms | 54.1 ms | - | `tier0_rules` |

## Cold vs. Warm Client Latency

| Path | Cold Count | Cold p50 | Cold p95 | Warm Count | Warm p50 | Warm p95 |
| :--- | -: | -: | -: | -: | -: | -: |
| `greeting` | 1 | 1488.5 ms | 1488.5 ms | 19 | 1220.9 ms | 1468.7 ms |
| `pleasantry` | 1 | 1415.4 ms | 1415.4 ms | 19 | 6416.6 ms | 25875.6 ms |
| `policy_faq` | 1 | 21640.3 ms | 21640.3 ms | 19 | 4648.5 ms | 21708.2 ms |
| `order_list_auth` | 1 | 14038.0 ms | 14038.0 ms | 19 | 11704.9 ms | 42678.7 ms |
| `order_id_auth` | 1 | 18550.3 ms | 18550.3 ms | 19 | 6496.2 ms | 24542.8 ms |
| `order_anon` | 1 | 4305.8 ms | 4305.8 ms | 19 | 1574.4 ms | 18840.0 ms |
| `out_of_scope` | 1 | 5052.4 ms | 5052.4 ms | 19 | 4725.3 ms | 29752.1 ms |
| `escalation` | 1 | 7684.8 ms | 7684.8 ms | 19 | 1196.2 ms | 6718.7 ms |

## Detailed Pipeline Step Breakdown (p50 / p95)

| Path | TTFB | Platform Overhead | Customer Auth | Ticket Upsert | A2A Handoff | FAQ Resolve | Search | Rerank | Model Armor |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `greeting` | 1036 / 1317 ms | 884 / 1096 ms | - | 350 / 399 ms | - | - | - | - | - |
| `pleasantry` | 4000 / 25138 ms | 862 / 1330 ms | - | 315 / 374 ms | 2936 / 14472 ms | 2752 / 27446 ms | 309 / 386 ms | 123 / 309 ms | - |
| `policy_faq` | 4471 / 21443 ms | 874 / 1148 ms | - | 244 / 292 ms | 3335 / 20044 ms | 3163 / 34468 ms | 276 / 365 ms | 123 / 138 ms | - |
| `order_list_auth` | 12243 / 42062 ms | 876 / 1369 ms | 226 / 234 ms | 260 / 302 ms | 6053 / 20026 ms | 5804 / 30824 ms | 300 / 477 ms | 119 / 180 ms | - |
| `order_id_auth` | 7131 / 23137 ms | 956 / 1080 ms | 228 / 249 ms | 253 / 300 ms | 4201 / 20029 ms | 4030 / 28823 ms | 290 / 384 ms | 119 / 132 ms | - |
| `order_anon` | 1330 / 18024 ms | 931 / 1413 ms | - | 246 / 290 ms | 122 / 284 ms | 0 / 0 ms | - | - | - |
| `out_of_scope` | 4516 / 29411 ms | 937 / 1492 ms | - | 374 / 431 ms | - | - | - | - | - |
| `escalation` | 1045 / 6651 ms | 897 / 2613 ms | - | 246 / 261 ms | - | - | - | - | - |

## Tier & Decision Distribution

| Path | Decisions | Tiers | Decision Match Rate |
| :--- | :--- | :--- | -: |
| `greeting` | auto_rag: 20 | tier0_rules: 20 | 100.0% |
| `pleasantry` | auto_rag: 19, escalate: 1 | tier0_rules: 10, tier2_flash: 10 | 95.0% |
| `policy_faq` | escalate: 4, auto_rag: 16 | tier1_router_s1: 20 | 80.0% |
| `order_list_auth` | auto_rag: 16, escalate: 4 | tier2_flash: 18, model_armor: 2 | 80.0% |
| `order_id_auth` | auto_rag: 15, escalate: 5 | tier1_router_s1: 15, tier2_flash: 5 | 75.0% |
| `order_anon` | auto_rag: 20 | tier2_flash: 5, tier1_router_s1: 15 | 100.0% |
| `out_of_scope` | reject_oos: 20 | tier2_flash: 20 | 100.0% |
| `escalation` | escalate: 20 | tier2_flash: 5, tier0_rules: 10, tier1_router_s1: 5 | 100.0% |
