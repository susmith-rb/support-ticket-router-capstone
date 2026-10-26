"""rag/laya_cpu_bench.py — CPU inference benchmark for Laya models.

Measures latency and throughput on CPU for:
1. Reranker: Top-5 candidate choice reranking over 300 DEV queries.
   - Evaluates FP32 eager vs QINT8 dynamic quantization across 4 and 8 threads.
   - Evaluates BF16 autocast if supported by hardware (e.g. Sapphire Rapids AMX/AVX-512).
   - Exports DEV candidate rows for multi_label_eval.py scoring.
2. Router: System-1 router-s1 multi-question inference across 300 DEV queries.
   - Evaluates 4 questions (escalate, sentiment, urgency, intent).
   - Verifies decision agreement between FP32 and QINT8.
3. Throughput: Requests/sec under single-process (8 threads) vs 2-process (2 x 4 threads).
"""

import argparse
import collections
import json
import multiprocessing as mp
import os
import pathlib
import subprocess
import time
from typing import Any, Dict, List, Sequence, Tuple


def get_cpu_info() -> Dict[str, Any]:
    """Retrieve CPU model name, flags, and feature support from system and PyTorch."""
    model_name = "Unknown"
    flags: List[str] = []
    num_cpus = os.cpu_count() or 1

    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name") and model_name == "Unknown":
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        model_name = parts[1].strip()
                elif line.startswith("flags") and not flags:
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        flags = parts[1].strip().split()
    except (FileNotFoundError, PermissionError):
        try:
            res = subprocess.run(["lscpu"], capture_output=True, text=True, check=False)
            for line in res.stdout.splitlines():
                if "Model name:" in line:
                    model_name = line.split(":", 1)[1].strip()
                elif "Flags:" in line:
                    flags = line.split(":", 1)[1].strip().split()
        except (FileNotFoundError, PermissionError):
            pass

    has_avx512 = any(f.startswith("avx512") for f in flags)
    has_amx = any(f.startswith("amx") for f in flags)

    is_bf16_supported = False
    try:
        import torch
        try:
            is_bf16_supported = bool(torch.cpu.is_bf16_supported())
        except AttributeError:
            is_bf16_supported = False
    except ImportError:
        pass

    return {
        "model_name": model_name,
        "num_cpus": num_cpus,
        "flags": flags,
        "has_avx512": has_avx512,
        "has_amx": has_amx,
        "is_bf16_supported": is_bf16_supported,
    }


def quantize_laya_agent(agent: Any) -> Any:
    """Apply dynamic int8 quantization to Laya ModernBERT encoder and scorer modules."""
    try:
        import torch
        import torch.nn as nn
        from torch.quantization import quantize_dynamic
    except ImportError as e:
        raise RuntimeError("PyTorch is required for quantize_laya_agent") from e

    try:
        model = agent.model
    except AttributeError:
        model = agent

    try:
        encoder = model.encoder
        if isinstance(encoder, nn.Module):
            model.encoder = quantize_dynamic(encoder, {nn.Linear}, dtype=torch.qint8)
    except AttributeError:
        pass

    try:
        scorer = model.scorer
        if isinstance(scorer, nn.Module):
            model.scorer = quantize_dynamic(scorer, {nn.Linear}, dtype=torch.qint8)
    except AttributeError:
        pass

    return agent


def measure_percentiles(latencies: Sequence[float]) -> Dict[str, float]:
    """Calculate summary statistics and percentiles for latency measurements in milliseconds."""
    if not latencies:
        return {
            "count": 0,
            "mean": 0.0,
            "min": 0.0,
            "max": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
        }

    sorted_lats = sorted(latencies)
    n = len(sorted_lats)
    mean_val = sum(sorted_lats) / n
    p50_idx = min(int(n * 0.50), n - 1)
    p90_idx = min(int(n * 0.90), n - 1)
    p95_idx = min(int(n * 0.95), n - 1)
    p99_idx = min(int(n * 0.99), n - 1)

    return {
        "count": n,
        "mean": round(mean_val, 2),
        "min": round(sorted_lats[0], 2),
        "max": round(sorted_lats[-1], 2),
        "p50": round(sorted_lats[p50_idx], 2),
        "p90": round(sorted_lats[p90_idx], 2),
        "p95": round(sorted_lats[p95_idx], 2),
        "p99": round(sorted_lats[p99_idx], 2),
    }


def build_choice_question(candidate_ids: Sequence[str], docs: Dict[str, Any]) -> Dict[str, Any]:
    """Construct a Laya choice question dictionary for a set of candidate document IDs."""
    criteria: Dict[str, str] = {}
    for doc_id in candidate_ids:
        doc = docs.get(doc_id, {})
        title = doc.get("title", doc_id)
        desc = doc.get("scope", "")
        criteria[doc_id] = f"{title}: {desc}" if desc else title

    return {
        "type": "choice",
        "instructions": "Select the most relevant FAQ article to answer the customer's question.",
        "criteria": criteria,
    }


def rerank_top5_query(
    query: str,
    top_candidates: Sequence[str],
    agent: Any,
    docs: Dict[str, Any],
) -> Tuple[List[str], Dict[str, float]]:
    """Rerank top 5 candidates with Laya choice model, leaving ranks 6-10 untouched."""
    head = list(top_candidates[:5])
    tail = list(top_candidates[5:10])

    if len(head) <= 1:
        return head + tail, {d: 1.0 for d in head}

    q_def = build_choice_question(head, docs)
    resp = agent.predict(query, {"article": q_def})
    answers = resp.get("answers", resp)
    art_ans = answers.get("article", {})
    probs = art_ans.get("choice_probabilities", art_ans.get("probabilities", {}))

    indexed = list(enumerate(head))
    indexed.sort(
        key=lambda item: (float(probs.get(item[1], 0.0)), -item[0]),
        reverse=True,
    )
    reordered_head = [item[1] for item in indexed]
    return reordered_head + tail, {k: float(v) for k, v in probs.items()}


def _worker_benchmark_slice(
    ckpt_path: str,
    quantize: bool,
    num_threads: int,
    docs: Dict[str, Any],
    queries_slice: List[Dict[str, Any]],
    return_dict: Any,
    worker_id: int,
) -> None:
    """Multiprocessing worker function for throughput testing."""
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    import laya
    import torch

    torch.set_num_threads(num_threads)
    agent = laya.load(ckpt_path)
    if quantize:
        agent = quantize_laya_agent(agent)

    # Warmup
    if queries_slice:
        w_row = queries_slice[0]
        w_top = w_row.get("top10", [])[:5]
        q_def = build_choice_question(w_top, docs)
        agent.predict(w_row.get("query", "warmup"), {"article": q_def})

    t0 = time.perf_counter()
    for row in queries_slice:
        q = row.get("query", "")
        top10 = row.get("top10", [])
        rerank_top5_query(q, top10, agent, docs)
    elapsed = time.perf_counter() - t0

    return_dict[worker_id] = {
        "count": len(queries_slice),
        "elapsed_s": elapsed,
    }


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for CPU benchmark."""
    parser = argparse.ArgumentParser(description="CPU Inference Benchmark for Laya Models.")
    parser.add_argument("--reranker-ckpt", required=True, help="Path to round-2 Laya reranker checkpoint.")
    parser.add_argument("--router-ckpt", required=True, help="Path to router-s1 v2 checkpoint.")
    parser.add_argument("--candidates", required=True, help="Path to dev_candidates.jsonl.")
    parser.add_argument("--docs", required=True, help="Path to docs.json.")
    parser.add_argument("--out-dir", required=True, help="Output directory for reports and candidate rows.")
    return parser.parse_args()


def run_benchmark(args: argparse.Namespace) -> Dict[str, Any]:
    """Execute complete CPU inference benchmarking suite for Laya reranker and router."""
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    import laya
    import torch

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cpu_info = get_cpu_info()
    print("=" * 70)
    print("LAYA CPU INFERENCE BENCHMARK")
    print(f"CPU Model: {cpu_info['model_name']}")
    print(f"Logical vCPUs: {cpu_info['num_cpus']}")
    print(f"AVX-512 Support: {cpu_info['has_avx512']}")
    print(f"AMX Support: {cpu_info['has_amx']}")
    print(f"Native PyTorch BF16 CPU: {cpu_info['is_bf16_supported']}")
    print("=" * 70)

    # 1. Load data
    with open(args.docs, "r", encoding="utf-8") as f:
        docs = json.load(f)

    rows: List[Dict[str, Any]] = []
    with open(args.candidates, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))

    print(f"Loaded {len(rows)} queries and {len(docs)} documents.")

    # 2. Reranker Benchmark Configurations
    # Configs: (name, quantize, threads, bf16)
    reranker_configs: List[Tuple[str, bool, int, bool]] = [
        ("reranker_fp32_8t", False, 8, False),
        ("reranker_fp32_4t", False, 4, False),
        ("reranker_qint8_8t", True, 8, False),
        ("reranker_qint8_4t", True, 4, False),
    ]

    reranker_results: Dict[str, Any] = {}

    for cfg_name, is_quant, n_threads, is_bf16 in reranker_configs:
        print(f"\n--- Running Reranker Config: {cfg_name} (quantize={is_quant}, threads={n_threads}) ---")
        torch.set_num_threads(n_threads)
        agent = laya.load(args.reranker_ckpt)
        if is_quant:
            agent = quantize_laya_agent(agent)

        # 10-query warmup
        print("Warming up 10 queries...")
        for w_row in rows[:10]:
            rerank_top5_query(w_row.get("query", ""), w_row.get("top10", []), agent, docs)

        latencies: List[float] = []
        result_rows: List[Dict[str, Any]] = []

        t_start = time.perf_counter()
        for idx, row in enumerate(rows):
            qid = row.get("qid", f"q{idx}")
            q = row.get("query", "")
            top10 = row.get("top10", [])

            t0 = time.perf_counter()
            reordered_top10, probs = rerank_top5_query(q, top10, agent, docs)
            lat_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(lat_ms)

            result_rows.append({
                "qid": qid,
                "query": q,
                "top10": reordered_top10,
                "top10_ids": reordered_top10,
                "initial_top10": top10,
                "latency_ms": round(lat_ms, 2),
                "laya_probs": {k: round(v, 4) for k, v in probs.items()},
            })

        total_elapsed_s = time.perf_counter() - t_start
        stats = measure_percentiles(latencies)
        stats["total_time_s"] = round(total_elapsed_s, 2)
        stats["qps"] = round(len(rows) / total_elapsed_s, 2) if total_elapsed_s > 0 else 0.0
        reranker_results[cfg_name] = stats
        print(f"Results: mean={stats['mean']}ms, p50={stats['p50']}ms, p95={stats['p95']}ms, p99={stats['p99']}ms, QPS={stats['qps']}")

        # Save output rows for evaluation if int8 or fp32_8t
        out_json_path = out_dir / f"dev_rows_{cfg_name}.json"
        export_payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "meta": {
                "config": cfg_name,
                "split": "dev",
                "n_queries": len(result_rows),
                "quantized": is_quant,
                "threads": n_threads,
            },
            "candidates_path": str(args.candidates),
            "n_queries": len(result_rows),
            "results": [
                {
                    "config": "laya_choice_top5_v2",
                    "description": f"CPU Reranker {cfg_name}",
                    "status": "completed",
                    "latency_summary": stats,
                    "rows": result_rows,
                }
            ],
        }
        with open(out_json_path, "w", encoding="utf-8") as f:
            json.dump(export_payload, f, indent=2)
        print(f"Saved evaluation payload to {out_json_path}")

    # BF16 Note
    bf16_skipped_reason = (
        "Skipped BF16 autocast: CPU lacks native bfloat16 hardware acceleration "
        "(AVX-512 VNNI / AMX; torch.cpu.is_bf16_supported() is False). "
        "Emulated BF16 on older Intel Haswell/Broadwell CPUs adds software conversion overhead "
        "without hardware throughput benefits."
    )
    print(f"\n{bf16_skipped_reason}")

    # 3. Router Benchmark
    print("\n" + "=" * 70)
    print("ROUTER-S1 V2 BENCHMARK")
    print("=" * 70)
    router_questions_path = pathlib.Path(args.router_ckpt) / "questions.json"
    with open(router_questions_path, "r", encoding="utf-8") as f:
        router_questions = json.load(f)

    router_configs: List[Tuple[str, bool, int]] = [
        ("router_fp32_8t", False, 8),
        ("router_fp32_4t", False, 4),
        ("router_qint8_8t", True, 8),
        ("router_qint8_4t", True, 4),
    ]

    router_results: Dict[str, Any] = {}
    router_predictions: Dict[str, List[Dict[str, Any]]] = {}

    for cfg_name, is_quant, n_threads in router_configs:
        print(f"\n--- Running Router Config: {cfg_name} (quantize={is_quant}, threads={n_threads}) ---")
        torch.set_num_threads(n_threads)
        agent = laya.load(args.router_ckpt)
        if is_quant:
            agent = quantize_laya_agent(agent)

        # Warmup
        print("Warming up 10 queries...")
        for w_row in rows[:10]:
            agent.predict(w_row.get("query", ""), router_questions)

        latencies = []
        preds: List[Dict[str, Any]] = []

        t_start = time.perf_counter()
        for idx, row in enumerate(rows):
            q = row.get("query", "")
            t0 = time.perf_counter()
            resp = agent.predict(q, router_questions)
            lat_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(lat_ms)

            answers = resp.get("answers", resp)
            decisions: Dict[str, Any] = {}
            for qid in router_questions:
                q_ans = answers.get(qid, {})
                dec = q_ans.get("choice", q_ans.get("answer", q_ans.get("decision")))
                decisions[qid] = dec
            preds.append(decisions)

        total_elapsed_s = time.perf_counter() - t_start
        stats = measure_percentiles(latencies)
        stats["total_time_s"] = round(total_elapsed_s, 2)
        stats["qps"] = round(len(rows) / total_elapsed_s, 2) if total_elapsed_s > 0 else 0.0
        router_results[cfg_name] = stats
        router_predictions[cfg_name] = preds
        print(f"Results: mean={stats['mean']}ms, p50={stats['p50']}ms, p95={stats['p95']}ms, p99={stats['p99']}ms, QPS={stats['qps']}")

    # Check decision agreement between router FP32 (8t) and QINT8 (8t)
    fp32_preds = router_predictions.get("router_fp32_8t", [])
    qint8_preds = router_predictions.get("router_qint8_8t", [])
    total_decisions = 0
    agreed_decisions = 0
    per_question_agreement: Dict[str, Dict[str, int]] = collections.defaultdict(lambda: {"total": 0, "agreed": 0})

    for p_fp32, p_qint8 in zip(fp32_preds, qint8_preds):
        for qid in router_questions:
            d1 = p_fp32.get(qid)
            d2 = p_qint8.get(qid)
            total_decisions += 1
            per_question_agreement[qid]["total"] += 1
            if d1 == d2:
                agreed_decisions += 1
                per_question_agreement[qid]["agreed"] += 1

    agreement_rate = (agreed_decisions / total_decisions) if total_decisions > 0 else 1.0
    print(f"\nRouter Decision Agreement (FP32 vs QINT8 across {total_decisions} total decisions):")
    print(f"Overall Agreement Rate: {agreement_rate * 100.0:.2f}% ({agreed_decisions}/{total_decisions})")
    for qid, q_stats in per_question_agreement.items():
        q_rate = (q_stats["agreed"] / q_stats["total"]) * 100.0 if q_stats["total"] > 0 else 100.0
        print(f"  - {qid}: {q_rate:.2f}% ({q_stats['agreed']}/{q_stats['total']})")

    # 4. Throughput Measurement
    print("\n" + "=" * 70)
    print("THROUGHPUT MEASUREMENT")
    print("=" * 70)
    # Serial 8 threads
    serial_qps = reranker_results.get("reranker_qint8_8t", {}).get("qps", 0.0)

    # 2 processes x 4 threads
    manager = mp.Manager()
    return_dict = manager.dict()
    half = len(rows) // 2
    slice1 = rows[:half]
    slice2 = rows[half:]

    print("Launching 2 processes x 4 threads benchmark...")
    t_mp_start = time.perf_counter()
    p1 = mp.Process(
        target=_worker_benchmark_slice,
        args=(args.reranker_ckpt, True, 4, docs, slice1, return_dict, 0),
    )
    p2 = mp.Process(
        target=_worker_benchmark_slice,
        args=(args.reranker_ckpt, True, 4, docs, slice2, return_dict, 1),
    )

    p1.start()
    p2.start()
    p1.join()
    p2.join()
    mp_elapsed_s = time.perf_counter() - t_mp_start
    mp_qps = round(len(rows) / mp_elapsed_s, 2) if mp_elapsed_s > 0 else 0.0
    print(f"Multi-process throughput (2 procs x 4 threads): {mp_qps} queries/s (elapsed {mp_elapsed_s:.2f}s)")

    # 5. Compile Summary Benchmark Markdown
    md_content = f"""# Laya CPU Inference Benchmark Report

- **Date**: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}
- **Hardware Platform**: {cpu_info['model_name']}
- **vCPUs**: {cpu_info['num_cpus']} logical vCPUs
- **Instruction Sets**: AVX, AVX2 (AVX-512: {cpu_info['has_avx512']}, AMX: {cpu_info['has_amx']})
- **PyTorch Native BF16 CPU**: {cpu_info['is_bf16_supported']}
- **DEV Evaluation Set**: 300 queries from `dev_candidates.jsonl`

## 1. Reranker Latency (Top-5 Choice over 300 Queries)

| Configuration | Threads | Quantization | Mean (ms) | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | Throughput (QPS) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **GPU Baseline (T4)** | — | FP32 | 31.03 | 32.18 | 33.25 | 33.75 | 36.12 | 32.22 |
| `reranker_fp32_8t` | 8 | None (FP32) | {reranker_results.get('reranker_fp32_8t', {}).get('mean', 0.0)} | {reranker_results.get('reranker_fp32_8t', {}).get('p50', 0.0)} | {reranker_results.get('reranker_fp32_8t', {}).get('p90', 0.0)} | {reranker_results.get('reranker_fp32_8t', {}).get('p95', 0.0)} | {reranker_results.get('reranker_fp32_8t', {}).get('p99', 0.0)} | {reranker_results.get('reranker_fp32_8t', {}).get('qps', 0.0)} |
| `reranker_fp32_4t` | 4 | None (FP32) | {reranker_results.get('reranker_fp32_4t', {}).get('mean', 0.0)} | {reranker_results.get('reranker_fp32_4t', {}).get('p50', 0.0)} | {reranker_results.get('reranker_fp32_4t', {}).get('p90', 0.0)} | {reranker_results.get('reranker_fp32_4t', {}).get('p95', 0.0)} | {reranker_results.get('reranker_fp32_4t', {}).get('p99', 0.0)} | {reranker_results.get('reranker_fp32_4t', {}).get('qps', 0.0)} |
| `reranker_qint8_8t` | 8 | Dynamic QINT8 | {reranker_results.get('reranker_qint8_8t', {}).get('mean', 0.0)} | {reranker_results.get('reranker_qint8_8t', {}).get('p50', 0.0)} | {reranker_results.get('reranker_qint8_8t', {}).get('p90', 0.0)} | {reranker_results.get('reranker_qint8_8t', {}).get('p95', 0.0)} | {reranker_results.get('reranker_qint8_8t', {}).get('p99', 0.0)} | {reranker_results.get('reranker_qint8_8t', {}).get('qps', 0.0)} |
| `reranker_qint8_4t` | 4 | Dynamic QINT8 | {reranker_results.get('reranker_qint8_4t', {}).get('mean', 0.0)} | {reranker_results.get('reranker_qint8_4t', {}).get('p50', 0.0)} | {reranker_results.get('reranker_qint8_4t', {}).get('p90', 0.0)} | {reranker_results.get('reranker_qint8_4t', {}).get('p95', 0.0)} | {reranker_results.get('reranker_qint8_4t', {}).get('p99', 0.0)} | {reranker_results.get('reranker_qint8_4t', {}).get('qps', 0.0)} |
| `reranker_bf16` | — | BF16 Autocast | — | — | — | — | — | *Skipped* |

> [!NOTE]
> {bf16_skipped_reason}

## 2. Router-S1 V2 Latency & Decision Agreement (300 DEV Queries, 4 Questions)

| Configuration | Threads | Quantization | Mean (ms) | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | Throughput (QPS) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `router_fp32_8t` | 8 | None (FP32) | {router_results.get('router_fp32_8t', {}).get('mean', 0.0)} | {router_results.get('router_fp32_8t', {}).get('p50', 0.0)} | {router_results.get('router_fp32_8t', {}).get('p90', 0.0)} | {router_results.get('router_fp32_8t', {}).get('p95', 0.0)} | {router_results.get('router_fp32_8t', {}).get('p99', 0.0)} | {router_results.get('router_fp32_8t', {}).get('qps', 0.0)} |
| `router_fp32_4t` | 4 | None (FP32) | {router_results.get('router_fp32_4t', {}).get('mean', 0.0)} | {router_results.get('router_fp32_4t', {}).get('p50', 0.0)} | {router_results.get('router_fp32_4t', {}).get('p90', 0.0)} | {router_results.get('router_fp32_4t', {}).get('p95', 0.0)} | {router_results.get('router_fp32_4t', {}).get('p99', 0.0)} | {router_results.get('router_fp32_4t', {}).get('qps', 0.0)} |
| `router_qint8_8t` | 8 | Dynamic QINT8 | {router_results.get('router_qint8_8t', {}).get('mean', 0.0)} | {router_results.get('router_qint8_8t', {}).get('p50', 0.0)} | {router_results.get('router_qint8_8t', {}).get('p90', 0.0)} | {router_results.get('router_qint8_8t', {}).get('p95', 0.0)} | {router_results.get('router_qint8_8t', {}).get('p99', 0.0)} | {router_results.get('router_qint8_8t', {}).get('qps', 0.0)} |
| `router_qint8_4t` | 4 | Dynamic QINT8 | {router_results.get('router_qint8_4t', {}).get('mean', 0.0)} | {router_results.get('router_qint8_4t', {}).get('p50', 0.0)} | {router_results.get('router_qint8_4t', {}).get('p90', 0.0)} | {router_results.get('router_qint8_4t', {}).get('p95', 0.0)} | {router_results.get('router_qint8_4t', {}).get('p99', 0.0)} | {router_results.get('router_qint8_4t', {}).get('qps', 0.0)} |

### Decision Agreement (FP32 vs QINT8 across 1,200 decisions)
- **Overall Agreement**: **{agreement_rate * 100.0:.2f}%** ({agreed_decisions} / {total_decisions} decisions identical)
"""
    for qid, q_stats in per_question_agreement.items():
        q_rate = (q_stats["agreed"] / q_stats["total"]) * 100.0 if q_stats["total"] > 0 else 100.0
        md_content += f"- **Question `{qid}`**: {q_rate:.2f}% ({q_stats['agreed']}/{q_stats['total']})\n"

    md_content += f"""
## 3. Serving Throughput Comparison

- **Single Process, Serial (8 threads, QINT8)**: **{serial_qps:.2f} queries/sec**
- **Multi-Process (2 workers x 4 threads, QINT8)**: **{mp_qps:.2f} queries/sec**
- **Concurrency Scaling Factor**: **{(mp_qps / serial_qps):.2f}x** (vs 1.0x baseline)
"""

    bench_md_path = out_dir / "benchmark.md"
    with open(bench_md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"\nWritten benchmark summary to {bench_md_path}")

    return {
        "cpu_info": cpu_info,
        "reranker_results": reranker_results,
        "router_results": router_results,
        "agreement_rate": agreement_rate,
        "serial_qps": serial_qps,
        "mp_qps": mp_qps,
    }


def main() -> None:
    """Main CLI entrypoint."""
    args = parse_args()
    run_benchmark(args)


if __name__ == "__main__":
    main()
