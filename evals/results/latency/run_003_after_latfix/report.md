# Live Per-Path Latency Benchmark Report

- **Total Requests**: 160
- **Total Errors**: 0 (0.0%)
- **Overall Decision-Match Rate**: 100.0%

## Per-Path Performance Overview

| Path | N | Errors | p50 Total | p95 Total | p50 Turn | p50 Routing | p50 GenAnswer | Dominant Tier |
| :--- | -: | -: | -: | -: | -: | -: | -: | :--- |
| `greeting` | 20 | 0 | 1273.0 ms | 1489.5 ms | 361.4 ms | 3.9 ms | - | `tier0_rules` |
| `pleasantry` | 20 | 0 | 1187.3 ms | 1295.6 ms | 352.6 ms | 2.0 ms | - | `tier0_rules` |
| `policy_faq` | 20 | 0 | 3581.8 ms | 5542.6 ms | 2707.7 ms | 143.0 ms | 1912.1 ms | `tier1_router_s1` |
| `order_list_auth` | 20 | 0 | 5903.1 ms | 9840.2 ms | 5079.3 ms | 145.2 ms | 4076.9 ms | `tier1_router_s1` |
| `order_id_auth` | 20 | 0 | 4399.4 ms | 5550.8 ms | 3385.2 ms | 147.9 ms | 2288.2 ms | `tier1_router_s1` |
| `order_anon` | 20 | 0 | 1302.5 ms | 1534.8 ms | 399.3 ms | 144.4 ms | - | `tier1_router_s1` |
| `out_of_scope` | 20 | 0 | 3227.2 ms | 4707.2 ms | 2261.4 ms | 1856.4 ms | - | `tier2_flash` |
| `escalation` | 20 | 0 | 1251.0 ms | 2881.4 ms | 315.9 ms | 78.1 ms | - | `tier0_rules` |

## Cold vs. Warm Client Latency

| Path | Cold Count | Cold p50 | Cold p95 | Warm Count | Warm p50 | Warm p95 |
| :--- | -: | -: | -: | -: | -: | -: |
| `greeting` | 1 | 1421.7 ms | 1421.7 ms | 19 | 1270.6 ms | 1496.2 ms |
| `pleasantry` | 1 | 1236.2 ms | 1236.2 ms | 19 | 1180.5 ms | 1296.3 ms |
| `policy_faq` | 1 | 3766.3 ms | 3766.3 ms | 19 | 3516.4 ms | 5588.8 ms |
| `order_list_auth` | 1 | 6933.0 ms | 6933.0 ms | 19 | 5895.3 ms | 9877.7 ms |
| `order_id_auth` | 1 | 4825.5 ms | 4825.5 ms | 19 | 4346.0 ms | 5580.1 ms |
| `order_anon` | 1 | 1356.1 ms | 1356.1 ms | 19 | 1302.1 ms | 1538.3 ms |
| `out_of_scope` | 1 | 3285.4 ms | 3285.4 ms | 19 | 3211.4 ms | 4748.2 ms |
| `escalation` | 1 | 2508.6 ms | 2508.6 ms | 19 | 1219.1 ms | 2895.4 ms |

## Detailed Pipeline Step Breakdown (p50 / p95)

| Path | TTFB | Platform Overhead | Customer Auth | Ticket Upsert | A2A Handoff | FAQ Resolve | Search | Rerank | Model Armor |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `greeting` | 1064 / 1352 ms | 915 / 1142 ms | - | 355 / 471 ms | - | - | - | - | - |
| `pleasantry` | 1015 / 1097 ms | 841 / 927 ms | - | 348 / 389 ms | - | - | - | - | - |
| `policy_faq` | 3452 / 5352 ms | 881 / 999 ms | - | 232 / 313 ms | 2568 / 4181 ms | 2513 / 4045 ms | 262 / 329 ms | 114 / 136 ms | - |
| `order_list_auth` | 5742 / 9645 ms | 896 / 1246 ms | 251 / 621 ms | 248 / 406 ms | 4931 / 8591 ms | 4771 / 8467 ms | 249 / 465 ms | 118 / 134 ms | - |
| `order_id_auth` | 4188 / 5349 ms | 881 / 1002 ms | 236 / 254 ms | 248 / 609 ms | 3181 / 4152 ms | 2992 / 3999 ms | 261 / 295 ms | 113 / 128 ms | - |
| `order_anon` | 1114 / 1345 ms | 907 / 1022 ms | - | 249 / 430 ms | 81 / 166 ms | 0 / 0 ms | - | - | - |
| `out_of_scope` | 3011 / 4510 ms | 875 / 993 ms | - | 353 / 457 ms | - | - | - | - | - |
| `escalation` | 1039 / 2688 ms | 877 / 1106 ms | - | 232 / 266 ms | - | - | - | - | - |

## Tier & Decision Distribution

| Path | Decisions | Tiers | Decision Match Rate |
| :--- | :--- | :--- | -: |
| `greeting` | auto_rag: 20 | tier0_rules: 20 | 100.0% |
| `pleasantry` | auto_rag: 20 | tier0_rules: 20 | 100.0% |
| `policy_faq` | auto_rag: 20 | tier1_router_s1: 20 | 100.0% |
| `order_list_auth` | auto_rag: 20 | tier1_router_s1: 20 | 100.0% |
| `order_id_auth` | auto_rag: 20 | tier1_router_s1: 20 | 100.0% |
| `order_anon` | auto_rag: 20 | tier1_router_s1: 20 | 100.0% |
| `out_of_scope` | reject_oos: 20 | tier2_flash: 20 | 100.0% |
| `escalation` | escalate: 20 | tier2_flash: 5, tier0_rules: 10, tier1_router_s1: 5 | 100.0% |
