#!/usr/bin/env python3
"""rag/rerank_ce.py — Cross-encoder reranker training, inference, and score fusion (PLAN7 Arm A).

Fine-tunes a cross-encoder model on graded relevance targets using MSE regression
on grade/2, and provides post-retrieval score fusion with retrieval rank priors:
    final_score = α · ce_score + (1 − α) · rank_prior

Justification for MSE regression on grade/2:
--------------------------------------------
In multi-relevant RAG retrieval, articles possess graded relevance:
  - Grade 2: Primary relevant FAQ article (direct answer)
  - Grade 1: Secondary relevant FAQ article (contextually relevant / disambiguation alternative)
  - Grade 0: Non-relevant negative / distractor article

MSE regression on normalized targets y ∈ {0.0, 0.5, 1.0} directly preserves the continuous
distinction between primary and secondary matches while training the cross-encoder linear
head. Unlike binary cross-entropy (which either discards secondary matches or treats them as
identical to primary hits), MSE regression directly correlates with graded nDCG@5 gains
(IDCG gain 2 for primary vs 1 for secondary).

# REQ-RAG-RETR-016
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from ticket_router.config.models import model_id


def grade_to_label(grade: Union[int, float]) -> float:
    """Map relevance grade {0, 1, 2} to normalized regression label in [0.0, 1.0].

    Grade 2 (primary) -> 1.0
    Grade 1 (secondary) -> 0.5
    Grade 0 (negative) -> 0.0
    """
    g = float(grade)
    label = g / 2.0
    return max(0.0, min(1.0, label))


def compute_rank_prior(
    rank: int, total: int = 10, mode: str = "reciprocal"
) -> float:
    """Compute normalized rank prior score in (0.0, 1.0] for a 0-indexed rank.

    Args:
        rank: 0-indexed rank of candidate (0 is rank-1, 1 is rank-2, etc.).
        total: total candidate pool size.
        mode: 'reciprocal' (1 / (rank + 1)) or 'linear' ((total - rank) / total).

    Returns:
        float score decreasing monotonically with rank.
    """
    if rank < 0:
        raise ValueError(f"rank must be non-negative, got {rank}")
    if total <= 0:
        raise ValueError(f"total must be positive, got {total}")

    if mode == "reciprocal":
        return 1.0 / (float(rank) + 1.0)
    if mode == "linear":
        return max(0.0, float(total - rank) / float(total))
    raise ValueError(f"Unsupported prior mode: {mode}. Use 'reciprocal' or 'linear'.")


def fuse_scores(
    ce_score: float,
    rank: int,
    alpha: float,
    total: int = 10,
    prior_mode: str = "reciprocal",
) -> float:
    """Fuse cross-encoder score with retrieval rank prior.

    Formula: final = α · ce + (1 − α) · rank_prior

    Args:
        ce_score: Cross-encoder predicted score.
        rank: 0-indexed rank in initial candidate list.
        alpha: Weight for cross-encoder score in [0.0, 1.0].
        total: Candidate pool size.
        prior_mode: 'reciprocal' or 'linear'.

    Returns:
        Fused final score.
    """
    if not (0.0 <= alpha <= 1.0):
        raise ValueError(f"alpha must be in [0.0, 1.0], got {alpha}")

    prior = compute_rank_prior(rank, total=total, mode=prior_mode)
    return alpha * float(ce_score) + (1.0 - alpha) * prior


def load_docs(docs_path: Union[str, Path]) -> Dict[str, str]:
    """Load docs catalog from JSON file mapping doc_id -> formatted text."""
    p = Path(docs_path)
    if not p.exists():
        raise FileNotFoundError(f"Docs catalog not found: {docs_path}")

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    docs_map: Dict[str, str] = {}
    for doc_id, doc_entry in data.items():
        if isinstance(doc_entry, dict):
            text = doc_entry.get("text") or doc_entry.get("title") or ""
        else:
            text = str(doc_entry)
        docs_map[doc_id] = text

    return docs_map


def load_candidates_data(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Load JSONL candidate records."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Candidates file not found: {path}")

    records: List[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                records.append(json.loads(line_str))
            except json.JSONDecodeError as e:
                sys.stderr.write(f"Warning: JSON decode error at {path}:{line_num}: {e}\n")
    return records


def rerank_candidates(
    query: str,
    candidate_ids: Sequence[str],
    docs_map: Dict[str, str],
    scorer: Callable[[List[Tuple[str, str]]], Sequence[float]],
    alpha: float = 1.0,
    prior_mode: str = "reciprocal",
) -> List[Tuple[str, float]]:
    """Rerank candidate document IDs for a single query using score fusion.

    Args:
        query: User inquiry text.
        candidate_ids: Sequence of candidate doc_ids in initial search rank order.
        docs_map: Mapping of doc_id -> article text.
        scorer: Callable that accepts list of (query, doc_text) pairs and returns scores.
        alpha: Score fusion weight in [0.0, 1.0].
        prior_mode: Prior mode for rank prior computation.

    Returns:
        List of (doc_id, fused_score) sorted descending by fused score.
        Ties are broken deterministically by original candidate rank.
    """
    if not candidate_ids:
        return []

    pairs = [(query, docs_map.get(cid, "")) for cid in candidate_ids]
    raw_scores = scorer(pairs)

    n_cands = len(candidate_ids)
    scored: List[Tuple[str, float, int]] = []
    for rank_idx, cid in enumerate(candidate_ids):
        ce_s = float(raw_scores[rank_idx])
        fused = fuse_scores(
            ce_score=ce_s,
            rank=rank_idx,
            alpha=alpha,
            total=n_cands,
            prior_mode=prior_mode,
        )
        scored.append((cid, fused, rank_idx))

    # Sort descending by fused score, breaking ties by initial rank_idx ascending
    scored.sort(key=lambda x: (-x[1], x[2]))
    return [(cid, fused) for cid, fused, _ in scored]


def rerank_dataset(
    candidate_rows: Sequence[Dict[str, Any]],
    docs_map: Dict[str, str],
    scorer: Callable[[List[Tuple[str, str]]], Sequence[float]],
    alphas: Sequence[float] = (0.3, 0.5, 0.7, 1.0),
    prior_mode: str = "reciprocal",
    config_prefix: str = "ce_minilm",
    model_name: Optional[str] = None,
    meta: Optional[str] = None,
) -> Dict[str, Any]:
    """Rerank an entire dataset across multiple fusion alphas, measuring latency.

    Produces output matching the ablation/dev_rows_phaseD.json schema.
    """
    resolved_model_name = model_name or model_id("ce_reranker_base")
    n_queries = len(candidate_rows)

    # Precompute CE scores for all queries to ensure latency measurements are fair
    # and independent of alpha sweeping
    per_query_latencies: List[float] = []
    scored_queries: List[Tuple[str, List[str], List[float]]] = []

    for row in candidate_rows:
        qid = str(row.get("qid", "")).strip()
        query = str(row.get("query", "")).strip()
        top_cands = (
            row.get("top10")
            or row.get("top10_ids")
            or row.get("candidates")
            or []
        )
        top_cands = [str(x) for x in top_cands]

        if not top_cands:
            scored_queries.append((qid, [], []))
            per_query_latencies.append(0.0)
            continue

        pairs = [(query, docs_map.get(cid, "")) for cid in top_cands]

        t0 = time.perf_counter()
        scores = list(scorer(pairs))
        dt_ms = (time.perf_counter() - t0) * 1000.0

        per_query_latencies.append(dt_ms)
        scored_queries.append((qid, top_cands, scores))

    # Compute latency summary
    valid_latencies = [l for l in per_query_latencies if l > 0.0] or [0.0]
    valid_latencies_sorted = sorted(valid_latencies)
    n_lat = len(valid_latencies_sorted)

    p50_idx = int(math.floor(0.50 * (n_lat - 1)))
    p90_idx = int(math.floor(0.90 * (n_lat - 1)))
    p95_idx = int(math.floor(0.95 * (n_lat - 1)))
    p99_idx = int(math.floor(0.99 * (n_lat - 1)))

    latency_summary = {
        "count": n_queries,
        "mean_ms": round(sum(valid_latencies) / max(1, len(valid_latencies)), 2),
        "min_ms": round(valid_latencies_sorted[0], 2),
        "max_ms": round(valid_latencies_sorted[-1], 2),
        "p50_ms": round(valid_latencies_sorted[p50_idx], 2),
        "p90_ms": round(valid_latencies_sorted[p90_idx], 2),
        "p95_ms": round(valid_latencies_sorted[p95_idx], 2),
        "p99_ms": round(valid_latencies_sorted[p99_idx], 2),
    }

    results: List[Dict[str, Any]] = []
    for alpha in alphas:
        cfg_name = f"{config_prefix}_a{alpha}"
        rows: List[Dict[str, Any]] = []

        for qid, cands, ce_scores in scored_queries:
            if not cands:
                rows.append({"qid": qid, "top10_ids": []})
                continue

            n_c = len(cands)
            fused_list: List[Tuple[str, float, int]] = []
            for rank_idx, cid in enumerate(cands):
                fused = fuse_scores(
                    ce_score=ce_scores[rank_idx],
                    rank=rank_idx,
                    alpha=alpha,
                    total=n_c,
                    prior_mode=prior_mode,
                )
                fused_list.append((cid, fused, rank_idx))

            fused_list.sort(key=lambda x: (-x[1], x[2]))
            reranked_ids = [cid for cid, _, _ in fused_list]
            rows.append({"qid": qid, "top10_ids": reranked_ids})

        results.append({
            "config": cfg_name,
            "alpha": alpha,
            "prior_mode": prior_mode,
            "status": "completed",
            "latency_summary": latency_summary,
            "rows": rows,
        })

    out_dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_name": resolved_model_name,
        "n_queries": n_queries,
        "results": results,
    }
    if meta:
        return {"meta": meta, **out_dict}
    return out_dict


class CrossEncoderScorer:
    """Wrapper around HuggingFace / SentenceTransformers model for batch inference."""

    def __init__(
        self,
        model_name_or_path: str,
        batch_size: int = 32,
        max_length: int = 256,
        device: str = "cpu",
    ):
        self.model_name_or_path = model_name_or_path
        self.batch_size = batch_size
        self.max_length = max_length
        self.device = device
        self._model = None
        self._tokenizer = None

    def _lazy_init(self):
        if self._model is not None:
            return
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path)
        self._model = AutoModelForSequenceClassification.from_pretrained(
            self.model_name_or_path, num_labels=1
        )
        self._model.to(self.device)
        self._model.eval()

    def score(self, pairs: List[Tuple[str, str]]) -> List[float]:
        """Score a list of (query, document_text) pairs."""
        if not pairs:
            return []
        self._lazy_init()
        import torch

        all_scores: List[float] = []
        for i in range(0, len(pairs), self.batch_size):
            batch = pairs[i : i + self.batch_size]
            queries = [p[0] for p in batch]
            docs = [p[1] for p in batch]

            inputs = self._tokenizer(
                queries,
                docs,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            with torch.no_grad():
                logits = self._model(**inputs).logits.squeeze(-1)
                if logits.dim() == 0:
                    scores = [float(logits.item())]
                else:
                    scores = logits.cpu().tolist()
                all_scores.extend(scores)

        return all_scores


def train_cross_encoder(
    train_file: str,
    val_file: str,
    docs_file: str,
    output_dir: str,
    model_name: Optional[str] = None,
    epochs: int = 3,
    batch_size: int = 16,
    lr: float = 2e-5,
    max_length: int = 256,
    seed: int = 42,
    num_threads: int = 8,
) -> Dict[str, Any]:
    """Train cross-encoder model on CPU using PyTorch and MSE regression loss.

    Target labels: grade / 2.0 (grade 2 -> 1.0, grade 1 -> 0.5, grade 0 -> 0.0).
    Early stopping selects checkpoint with lowest validation loss.
    """
    resolved_model_name = model_name or model_id("ce_reranker_base")
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup

    torch.manual_seed(seed)
    random.seed(seed)
    if num_threads > 0:
        torch.set_num_threads(num_threads)

    docs_map = load_docs(docs_file)
    train_raw = load_candidates_data(train_file)
    val_raw = load_candidates_data(val_file)

    # Flatten into (query, doc_text, label) triples
    train_pairs: List[Tuple[str, str, float]] = []
    for r in train_raw:
        q = r["query"]
        rels = r.get("rels", {})
        cands = r.get("candidates", [])
        for cid in cands:
            grade = rels.get(cid, 0)
            label = grade_to_label(grade)
            train_pairs.append((q, docs_map.get(cid, ""), label))

    val_pairs: List[Tuple[str, str, float]] = []
    for r in val_raw:
        q = r["query"]
        rels = r.get("rels", {})
        cands = r.get("candidates", [])
        for cid in cands:
            grade = rels.get(cid, 0)
            label = grade_to_label(grade)
            val_pairs.append((q, docs_map.get(cid, ""), label))

    sys.stdout.write(
        f"Training set: {len(train_raw)} queries, {len(train_pairs)} pairs\n"
        f"Validation set: {len(val_raw)} queries, {len(val_pairs)} pairs\n"
    )

    tokenizer = AutoTokenizer.from_pretrained(resolved_model_name)
    model = AutoModelForSequenceClassification.from_pretrained(resolved_model_name, num_labels=1)
    model.train()

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    n_train_batches = math.ceil(len(train_pairs) / batch_size)
    total_steps = n_train_batches * epochs
    warmup_steps = int(0.1 * total_steps)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps, num_training_steps=total_steps
    )
    loss_fn = torch.nn.MSELoss()

    best_val_loss = float("inf")
    best_epoch = -1
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    training_start = time.time()
    epoch_records = []

    for epoch in range(1, epochs + 1):
        epoch_t0 = time.time()
        model.train()
        train_loss_sum = 0.0
        n_batches = 0

        # Deterministic shuffle per epoch
        epoch_pairs = list(train_pairs)
        random.seed(seed + epoch)
        random.shuffle(epoch_pairs)

        for step, i in enumerate(range(0, len(epoch_pairs), batch_size), 1):
            batch_slice = epoch_pairs[i : i + batch_size]
            queries = [item[0] for item in batch_slice]
            docs = [item[1] for item in batch_slice]
            labels = torch.tensor([item[2] for item in batch_slice], dtype=torch.float32)

            batch = tokenizer(
                queries,
                docs,
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            outputs = model(**batch).logits.squeeze(-1)
            loss = loss_fn(outputs, labels)

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()

            train_loss_sum += float(loss.item())
            n_batches += 1

            if step % 200 == 0 or step == n_train_batches:
                avg_l = train_loss_sum / n_batches
                sys.stdout.write(
                    f"Epoch {epoch}/{epochs} | Step {step}/{n_train_batches} | Train MSE: {avg_l:.4f}\n"
                )

        train_loss = train_loss_sum / max(1, n_batches)

        # Validation evaluation
        model.eval()
        val_loss_sum = 0.0
        val_batches = 0
        with torch.no_grad():
            for j in range(0, len(val_pairs), batch_size * 2):
                batch_slice = val_pairs[j : j + batch_size * 2]
                queries = [item[0] for item in batch_slice]
                docs = [item[1] for item in batch_slice]
                labels = torch.tensor([item[2] for item in batch_slice], dtype=torch.float32)

                batch = tokenizer(
                    queries,
                    docs,
                    padding=True,
                    truncation=True,
                    max_length=max_length,
                    return_tensors="pt",
                )
                outputs = model(**batch).logits.squeeze(-1)
                v_loss = loss_fn(outputs, labels)
                val_loss_sum += float(v_loss.item())
                val_batches += 1

        val_loss = val_loss_sum / max(1, val_batches)
        epoch_dt = time.time() - epoch_t0
        sys.stdout.write(
            f"=== Epoch {epoch} Complete | Train MSE: {train_loss:.4f} | Val MSE: {val_loss:.4f} | Time: {epoch_dt:.1f}s ===\n"
        )

        rec = {
            "epoch": epoch,
            "train_loss": train_loss,
            "val_loss": val_loss,
            "time_sec": epoch_dt,
        }
        epoch_records.append(rec)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_epoch = epoch
            sys.stdout.write(f"-> New best validation loss {val_loss:.4f}. Saving model to {out_path}...\n")
            model.save_pretrained(out_path)
            tokenizer.save_pretrained(out_path)

    total_training_time = time.time() - training_start
    manifest = {
        "model_name": resolved_model_name,
        "best_epoch": best_epoch,
        "best_val_loss": best_val_loss,
        "epochs": epochs,
        "batch_size": batch_size,
        "learning_rate": lr,
        "total_training_time_sec": total_training_time,
        "epoch_records": epoch_records,
    }
    with open(out_path / "training_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    sys.stdout.write(
        f"Training complete in {total_training_time:.1f}s. Best Epoch: {best_epoch} (Val MSE: {best_val_loss:.4f})\n"
    )
    return manifest


def parse_args():
    parser = argparse.ArgumentParser(
        description="Cross-Encoder Reranker Training and Score Fusion (PLAN7 Arm A)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Train sub-command
    train_parser = subparsers.add_parser("train", help="Train cross-encoder model on CPU.")
    train_parser.add_argument("--train-file", required=True, help="Path to train.jsonl")
    train_parser.add_argument("--val-file", required=True, help="Path to val.jsonl")
    train_parser.add_argument("--docs-file", required=True, help="Path to docs.json")
    train_parser.add_argument(
        "--model-name",
        default=model_id("ce_reranker_base"),
        help="Base cross-encoder model name or HuggingFace ID",
    )
    train_parser.add_argument("--output-dir", required=True, help="Directory to save fine-tuned model")
    train_parser.add_argument("--epochs", type=int, default=3, help="Training epochs")
    train_parser.add_argument("--batch-size", type=int, default=16, help="Batch size")
    train_parser.add_argument("--lr", type=float, default=2e-5, help="Learning rate")
    train_parser.add_argument("--max-length", type=int, default=256, help="Max sequence length")
    train_parser.add_argument("--seed", type=int, default=42, help="Random seed")
    train_parser.add_argument("--num-threads", type=int, default=8, help="PyTorch CPU threads")

    # Rerank sub-command
    rerank_parser = subparsers.add_parser("rerank", help="Rerank candidate sets with fusion.")
    rerank_parser.add_argument("--model-path", required=True, help="Path or HF ID of trained model")
    rerank_parser.add_argument("--candidates-file", required=True, help="Path to dev_candidates.jsonl")
    rerank_parser.add_argument("--docs-file", required=True, help="Path to docs.json")
    rerank_parser.add_argument(
        "--alphas",
        default="0.3,0.5,0.7,1.0",
        help="Comma-separated score fusion weights alpha (default: 0.3,0.5,0.7,1.0)",
    )
    rerank_parser.add_argument(
        "--prior-mode",
        choices=["reciprocal", "linear"],
        default="reciprocal",
        help="Rank prior calculation mode (default: reciprocal)",
    )
    rerank_parser.add_argument(
        "--config-prefix",
        default="ce_minilm",
        help="Config name prefix in results (default: ce_minilm)",
    )
    rerank_parser.add_argument(
        "--batch-size", type=int, default=32, help="Inference batch size"
    )
    rerank_parser.add_argument(
        "--max-length", type=int, default=256, help="Max sequence length"
    )
    rerank_parser.add_argument(
        "--meta",
        default="",
        help="Optional top-level meta string or description in output JSON",
    )
    rerank_parser.add_argument("--output-file", required=True, help="Path to save output JSON")

    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "train":
        train_cross_encoder(
            train_file=args.train_file,
            val_file=args.val_file,
            docs_file=args.docs_file,
            output_dir=args.output_dir,
            model_name=args.model_name,
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
            max_length=args.max_length,
            seed=args.seed,
            num_threads=args.num_threads,
        )
    elif args.command == "rerank":
        alphas = [float(x.strip()) for x in args.alphas.split(",") if x.strip()]
        docs_map = load_docs(args.docs_file)
        candidates = load_candidates_data(args.candidates_file)

        scorer = CrossEncoderScorer(
            model_name_or_path=args.model_path,
            batch_size=args.batch_size,
            max_length=args.max_length,
            device="cpu",
        )

        output_data = rerank_dataset(
            candidate_rows=candidates,
            docs_map=docs_map,
            scorer=scorer.score,
            alphas=alphas,
            prior_mode=args.prior_mode,
            config_prefix=args.config_prefix,
            model_name=args.model_path,
            meta=args.meta if args.meta else None,
        )

        out_p = Path(args.output_file)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)

        sys.stdout.write(f"Reranked {len(candidates)} queries across {len(alphas)} configs -> {out_p}\n")
        sys.stdout.write(f"Latency: {output_data['results'][0]['latency_summary']}\n")


if __name__ == "__main__":
    main()
