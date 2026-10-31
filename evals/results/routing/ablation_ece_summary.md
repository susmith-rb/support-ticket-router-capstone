# Router Cascade 4-Arm Ablation & 15-Bin Calibration Report

## 1. Four-Arm Router Architecture Ablation (`golden_all_model_outputs.csv`, `n=300`)

- **Ground Truth**: Independent cross-family Claude teacher consensus (`Opus` + `Sonnet`) to avoid self-preference bias when comparing `Laya` (`router-s1`) and `Gemini` (`gemini-3.8-flash`).
- **Production Operating Thresholds**: `tau_low = 0.15`, `tau_esc = 0.50` (add-only escalation across Tier 0, Tier 1, and Tier 2).

| Architecture Arm | Strict Angry/Urgent Recall (95% Wilson LB) | Escalation Recall | Escalation Precision | Neutral False-Esc Rate | Intent Acc (Non-T0) | Intent Macro-F1 (Non-T0) | Tier-2 LLM Share | Route p95 Latency | Unit Cost / 1k (@2M/mo) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Arm 1: Rules-Only (Tier 0 + Order Regex)** | `0.1889` (`17/90`, LB `0.1214`) | `0.0924` | `1.0000` | `0.0000` | `0.3300` (`0.3251`) | `0.0992` (`0.0981`) | `0.0%` | `0.8 ms` | `$0.00` |
| **Arm 2: Laya-Only (Tier 1 router-s1 Encoder)** | `0.8778` (`79/90`, LB `0.7943`) | `0.7065` | `0.9353` | `0.0282` | `0.8533` (`0.8533`) | `0.8077` (`0.8077`) | `0.0%` | `77.0 ms` | `$0.27` |
| **Arm 3: Gemini-Only (100% LLM Routing)** | `1.0000` (`90/90`, LB `0.9591`) | `0.7935` | `0.9669` | `0.0282` | `0.9233` (`0.9233`) | `0.8753` (`0.8753`) | `100.0%` | `3850.0 ms` | `$1.68` |
| **Arm 4: Hybrid Cascade (Tier 0 + Tier 1 Laya + Tier 2 Gemini)** | `0.9889` (`89/90`, LB `0.9397`) | `0.8152` | `0.9375` | `0.0423` | `0.8433` (`0.8693`) | `0.8053` (`0.8247`) | `33.0%` | `1420.0 ms` | `$0.82` |

### Key Findings
- **Safety Recall**: Neither `rules_only` (`18.9%`) nor standalone `laya_only` (`87.8%`) meets the `>= 0.98` strict angry/urgent escalation hard gate. The **Hybrid Cascade** reaches **`98.89%` (`89/90`)** strict recall and **`81.52%` overall escalation recall** (exceeding standalone `gemini_only` `79.35%` via the add-only escalation invariant `escalate = t0 OR t1 OR t2`).
- **Determinism, Latency & Quota Shielding**: The Hybrid Cascade deflects **`67.0%`** of tickets away from Tier-2 Gemini LLM calls on the golden set (`64.0%` on `routing_v2.1.0/dev`), cutting median routing latency from multi-second LLM calls to **`54–77 ms`** on definitive Tier-1 traffic while isolating `~65%` of traffic from Gemini `429 RESOURCE_EXHAUSTED` events.

---

## 2. 15-Bin Expected Calibration Error (`ECE`) & Reliability Summary

| Dataset & Head | Samples (`n`) | Bins | Temperature `T` | 15-Bin ECE | Max Calibration Error (`MCE`) | Target Gate |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `router-s1` v2 Held-Out Test — **Intent Head** (`eval_test.json`) | `646` | `15` | `1.18` | **`0.0526`** | — | `<= 0.08` (PASS) |
| `router-s1` v2 Held-Out Test — **Sentiment Head** (`eval_test.json`) | `646` | `15` | `1.18` | **`0.0865`** | — | Diagnostic |
| `routing_v2.1.0/dev` (`run_027_latfix_replay_dev`) — **Tier-1 Deciding Intent** | `313` | `15` | `1.18` | **`0.0694`** | `0.5417` | `<= 0.08` (PASS) |
| `routing_v2.1.0/dev` (`run_027_latfix_replay_dev`) — **Tier-1 All Neural Intent** | `500` | `15` | `1.18` | **`0.0720`** | `0.3768` | `<= 0.08` (PASS) |
| `routing_v2.1.0/dev` (`run_027_latfix_replay_dev`) — **Tier-1 Escalation Head** | `500` | `15` | `1.18` | **`0.3033`** | `0.4921` | Diagnostic |
| `golden_all_model_outputs` — **Tier-1 Escalation Head** | `300` | `15` | `1.18` | **`0.1301`** | `0.3578` | Diagnostic |

### 15-Bin Reliability Table (`routing_v2.1.0/dev` Tier-1 Deciding Intent, `n=313`)

| Bin | Confidence Interval | Count | Mean Confidence | Empirical Accuracy | `|Acc - Conf|` |
| :---: | :---: | :---: | :---: | :---: | :---: |
| `05` | `[0.333, 0.400)` | `1` | `0.3713` | `0.0000` | `0.3713` |
| `06` | `[0.400, 0.467)` | `1` | `0.4583` | `1.0000` | `0.5417` |
| `07` | `[0.467, 0.533)` | `3` | `0.4983` | `0.3333` | `0.1650` |
| `08` | `[0.533, 0.600)` | `4` | `0.5644` | `0.2500` | `0.3144` |
| `09` | `[0.600, 0.667)` | `9` | `0.6393` | `0.6667` | `0.0274` |
| `10` | `[0.667, 0.733)` | `12` | `0.7030` | `0.5833` | `0.1197` |
| `11` | `[0.733, 0.800)` | `15` | `0.7727` | `0.7333` | `0.0393` |
| `12` | `[0.800, 0.867)` | `24` | `0.8293` | `0.7083` | `0.1210` |
| `13` | `[0.867, 0.933)` | `37` | `0.9026` | `0.8649` | `0.0378` |
| `14` | `[0.933, 1.000]` | `207` | `0.9831` | `0.9227` | `0.0603` |

## 3. Production Cascade Summary on `routing_v2.1.0/dev` (`n=519`)

- **Routing Correctness**: `0.7380`
- **Intent Accuracy / Macro-F1 (`all`, `n=519`)**: `0.8651` / `0.8500`
- **Intent Accuracy / Macro-F1 (`verified C2`, `n=108`)**: `0.8021` / `0.7623`
- **Escalation Recall / Precision / F1**: `0.9442` / `0.6790` / `0.7899`
- **Taxonomy Validity Rate**: `1.0000`
- **Deciding Tier Shares**: Tier 0 `3.7%`, Tier 1 `60.3%`, Tier 2 `36.0%`
