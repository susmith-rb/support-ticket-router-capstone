# Live Per-Path Latency Benchmark Report

- **Total Requests**: 160
- **Total Errors**: 0 (0.0%)
- **Overall Decision-Match Rate**: 100.0%

## Per-Path Performance Overview

| Path | N | Errors | p50 Total | p95 Total | p50 Turn | p50 Routing | p50 GenAnswer | Dominant Tier |
| :--- | -: | -: | -: | -: | -: | -: | -: | :--- |
| `greeting` | 20 | 0 | 2440.8 ms | 2819.0 ms | 834.3 ms | 376.5 ms | - | `tier0_rules` |
| `pleasantry` | 20 | 0 | 2234.1 ms | 2502.9 ms | 777.1 ms | 359.4 ms | - | `tier0_rules` |
| `policy_faq` | 20 | 0 | 4827.8 ms | 6941.1 ms | 3433.2 ms | 571.3 ms | 2323.6 ms | `tier1_router_s1` |
| `order_list_auth` | 20 | 0 | 7096.3 ms | 8174.1 ms | 5568.4 ms | 842.1 ms | 3758.9 ms | `tier1_router_s1` |
| `order_id_auth` | 20 | 0 | 5706.1 ms | 10505.7 ms | 4165.2 ms | 809.1 ms | 2389.9 ms | `tier1_router_s1` |
| `order_anon` | 20 | 0 | 2397.6 ms | 2738.3 ms | 886.4 ms | 577.3 ms | - | `tier1_router_s1` |
| `out_of_scope` | 20 | 0 | 4422.2 ms | 5800.5 ms | 2937.9 ms | 2458.1 ms | - | `tier2_flash` |
| `escalation` | 20 | 0 | 2684.2 ms | 4826.7 ms | 1106.4 ms | 605.8 ms | - | `tier0_rules` |

## Cold vs. Warm Client Latency

| Path | Cold Count | Cold p50 | Cold p95 | Warm Count | Warm p50 | Warm p95 |
| :--- | -: | -: | -: | -: | -: | -: |
| `greeting` | 1 | 2600.6 ms | 2600.6 ms | 19 | 2434.3 ms | 2870.6 ms |
| `pleasantry` | 1 | 2105.8 ms | 2105.8 ms | 19 | 2234.4 ms | 2506.9 ms |
| `policy_faq` | 1 | 4299.4 ms | 4299.4 ms | 19 | 4850.1 ms | 6947.6 ms |
| `order_list_auth` | 1 | 6988.5 ms | 6988.5 ms | 19 | 7150.2 ms | 8209.3 ms |
| `order_id_auth` | 1 | 4801.9 ms | 4801.9 ms | 19 | 5744.2 ms | 10877.3 ms |
| `order_anon` | 1 | 2415.7 ms | 2415.7 ms | 19 | 2397.5 ms | 2739.4 ms |
| `out_of_scope` | 1 | 3562.9 ms | 3562.9 ms | 19 | 4436.7 ms | 5828.3 ms |
| `escalation` | 1 | 4568.6 ms | 4568.6 ms | 19 | 2664.9 ms | 4891.8 ms |

## Detailed Pipeline Step Breakdown (p50 / p95)

| Path | TTFB | Platform Overhead | Customer Auth | Ticket Upsert | A2A Handoff | FAQ Resolve | Search | Rerank | Model Armor |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `greeting` | 2081 / 2288 ms | 1494 / 1819 ms | - | 445 / 745 ms | - | - | - | - | - |
| `pleasantry` | 1898 / 2167 ms | 1411 / 1604 ms | - | 423 / 616 ms | - | - | - | - | - |
| `policy_faq` | 4548 / 6592 ms | 1447 / 1528 ms | - | 309 / 397 ms | 2852 / 4978 ms | 2714 / 4826 ms | 204 / 344 ms | 126 / 165 ms | - |
| `order_list_auth` | 6725 / 7773 ms | 1486 / 1640 ms | 311 / 448 ms | 300 / 476 ms | 4388 / 5494 ms | 4230 / 5337 ms | 193 / 238 ms | 128 / 156 ms | - |
| `order_id_auth` | 5399 / 10215 ms | 1505 / 1815 ms | 319 / 373 ms | 310 / 388 ms | 3118 / 8139 ms | 2933 / 7975 ms | 191 / 252 ms | 127 / 250 ms | - |
| `order_anon` | 2053 / 2384 ms | 1470 / 1622 ms | - | 301 / 408 ms | 122 / 279 ms | 0 / 0 ms | - | - | - |
| `out_of_scope` | 4090 / 5430 ms | 1422 / 1600 ms | - | 442 / 611 ms | - | - | - | - | - |
| `escalation` | 2315 / 4527 ms | 1463 / 1613 ms | - | 314 / 431 ms | - | - | - | - | - |

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
