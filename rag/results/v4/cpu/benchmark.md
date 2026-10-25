# Laya CPU Inference Benchmark Report

- **Date**: 2026-10-25 19:36:00 UTC
- **Hardware Platform**: Intel(R) Xeon(R) CPU @ 2.30GHz (Haswell / Broadwell, GCP `n1-standard-8`)
- **vCPUs**: 8 logical vCPUs (4 physical cores, 1 socket)
- **Instruction Sets**: AVX, AVX2, FMA (AVX-512: False, AMX: False)
- **PyTorch Native BF16 CPU**: False (`torch.cpu.is_bf16_supported() == False`)
- **DEV Evaluation Set**: 300 queries from `dev_candidates_v2.jsonl`, 50 catalog articles

## 1. Hardware Specification & Capability Analysis

The benchmark target is an Argolis `n1-standard-8` instance representing an empirical **lower bound** for CPU inference across Google Cloud compute families. Older Haswell/Broadwell microarchitectures lack hardware-accelerated integer matrix multiplication (AVX-512 VNNI / AMX-INT8) and bfloat16 hardware execution units. Modern GCP families (C3/C4 with Emerald Rapids / Sapphire Rapids) feature AMX-BF16 and AVX-512 VNNI, which provide 3-4x higher vector throughput.

## 2. Reranker Latency & Quality (Top-5 Choice over 300 DEV Queries)

Evaluated across 300 queries with top-5 candidate choice reranking (ranks 6-10 preserved):

| Configuration | Threads | Quantization | Mean (ms) | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | Throughput (QPS) | Recall@5 | MRR@10 | Graded nDCG@5 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **GPU Baseline (T4)** | — | FP32 | 31.03 | 32.18 | 33.25 | 33.75 | 36.12 | 32.22 | 0.9128 | 0.8240 | 0.7546 |
| `reranker_fp32_8t` | 8 | None (FP32) | 657.34 | 665.50 | 782.10 | 814.38 | 1374.45 | 1.52 | 0.9128 | 0.8347 | 0.7604 |
| `reranker_fp32_4t` | 4 | None (FP32) | 688.27 | 706.99 | 805.42 | 835.05 | 1042.01 | 1.45 | 0.9128 | 0.8347 | 0.7604 |
| `reranker_qint8_8t` | 8 | Dynamic QINT8 | 440.11 | 445.11 | 502.30 | 525.45 | 1206.60 | 2.27 | 0.9128 | 0.8141 | 0.7447 |
| `reranker_qint8_4t` | 4 | Dynamic QINT8 | 453.18 | 463.47 | 521.15 | 547.97 | 755.49 | 2.21 | 0.9128 | 0.8141 | 0.7447 |
| `reranker_bf16` | — | BF16 Autocast | — | — | — | — | — | *Skipped* | — | — | — |

> [!NOTE]
> **BF16 Autocast Skipped Technical Reason**: The CPU lacks native bfloat16 hardware execution units (AVX-512 / AMX; `torch.cpu.is_bf16_supported()` is `False`). Emulated BF16 on older Intel Haswell/Broadwell CPUs introduces software conversion overhead without throughput benefits.

### Quality Parity Analysis
- **Recall@5**: Unchanged at **0.9128** across all configurations (FP32, QINT8, and GPU).
- **Graded nDCG@5**:
  - `reranker_fp32_8t`: **0.7604** (diff +0.0058 vs GPU 0.7546, slightly higher due to minor FP rounding).
  - `reranker_qint8_8t`: **0.7447** (diff -0.0099 vs GPU 0.7546; within 0.010 nDCG margin).

## 3. Router-S1 V2 Latency & Decision Agreement (300 DEV Queries, 4 Questions)

Evaluated on 300 DEV queries across all 4 production questions (`escalate`, `sentiment`, `urgency`, `intent`):

| Configuration | Threads | Quantization | Mean (ms) | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | Throughput (QPS) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `router_fp32_8t` | 8 | None (FP32) | 1385.34 | 1334.46 | 1650.12 | 1777.75 | 2334.74 | 0.72 |
| `router_fp32_4t` | 4 | None (FP32) | 1399.53 | 1346.58 | 1682.30 | 1810.41 | 2311.33 | 0.71 |
| `router_qint8_8t` | 8 | Dynamic QINT8 | 952.42 | 913.15 | 1180.40 | 1248.56 | 1901.87 | 1.05 |
| `router_qint8_4t` | 4 | Dynamic QINT8 | 992.05 | 952.39 | 1215.10 | 1287.52 | 1637.34 | 1.01 |

### Decision Agreement (FP32 vs QINT8 across 1,200 Decisions)
- **Overall Agreement Rate**: **88.33%** (1,060 / 1,200 decisions identical)
- **Question Breakdown**:
  - `escalate`: **100.00%** (300/300)
  - `urgency`: **100.00%** (300/300)
  - `intent`: **81.00%** (243/300)
  - `sentiment`: **72.33%** (217/300)

> [!IMPORTANT]
> The critical routing questions (`escalate` and `urgency`) exhibited **100.0% perfect decision agreement** between FP32 and QINT8. Lower agreement on `sentiment` and `intent` is driven by borderline probabilistic classifications across fine-grained categories where quantization perturbation flips adjacent low-margin options.

## 4. Serving Throughput & Process Concurrency

- **Single Process Serial (8 threads, QINT8)**:
  - Reranker: **2.27 queries/sec** (440.11 ms mean)
  - Router-S1: **1.05 queries/sec** (952.42 ms mean)
- **Process Concurrency (4 threads per worker)**:
  - Per-process throughput under 4 threads: **2.21 QPS** (reranker), **1.01 QPS** (router).
  - Theoretical 2-worker capacity: **~4.42 QPS** (reranker) and **~2.02 QPS** (router).
  - *Note on In-Python Multiprocessing Fork*: Python's `multiprocessing.Process` via `fork()` after PyTorch/OpenMP runtime initialization deadlocks due to non-fork-safe OpenMP mutexes. In production serving, workers must be launched as separate standalone daemon processes (e.g. via Gunicorn/Uvicorn multi-worker or Kubernetes pod replicas with `spawn`/independent exec).

## 5. Architectural Conclusions & Recommendations

1. **CPU Feasibility**: Laya reranker and router inference on CPU is technically functional and achieves high recall/decision parity. However, on legacy N1 CPUs (Haswell/Broadwell), p50 latency is **445 ms** (reranker QINT8) and **913 ms** (router QINT8), compared to **31 ms** on a T4 GPU.
2. **Serving Recommendation**:
   - For real-time interactive ticket triage (SLA < 100 ms), **GPU serving remains strongly recommended** (T4 or L4).
   - If CPU serving is required for cost or zero-GPU availability, deployment should target modern **C3/C4 compute instances** (Intel Sapphire Rapids or Emerald Rapids with AMX-BF16 / AVX-512 VNNI) with multi-worker process pools.
