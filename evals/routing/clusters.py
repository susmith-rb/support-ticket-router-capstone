"""Failure clustering and representative error analysis.

# REQ-REVAL-017: Failure clusters, representative errors, explain CLI.
Deterministically clusters incorrect routing decisions:
- Precedence error categorization:
  error > degraded > invalid_label > missed_escalation > wrong_queue > false_escalation > wrong_intent
- Hierarchical group keys: (error_type, expected->predicted, deciding_tier, primary_scenario)
- Pure-Python TF-IDF vectorization and cosine average-link agglomeration within keys
- Cluster severity weighting (size * severity) and signature extraction (top-5 TF-IDF terms)
- Medoid-first representative ticket selection
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from evals.routing.schema import RoutingCase
from ticket_router.config.taxonomy import load_taxonomy

SEVERITY_WEIGHTS: Dict[str, int] = {
    "missed_escalation": 5,
    "wrong_queue": 3,
    "degraded": 3,
    "invalid_label": 3,
    "error": 3,
    "false_escalation": 2,
    "wrong_intent": 1,
}

_RE_TOKEN = re.compile(r"\b[a-zA-Z0-9_]{2,}\b")

_STOPWORDS: Set[str] = {
    "a", "about", "all", "an", "and", "any", "are", "as", "at", "be", "been",
    "but", "by", "can", "do", "for", "from", "had", "has", "have", "he", "her",
    "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my",
    "no", "not", "of", "on", "or", "our", "out", "please", "so", "some", "that",
    "the", "their", "them", "then", "there", "they", "this", "to", "up", "us",
    "was", "we", "were", "what", "when", "where", "which", "who", "will", "with",
    "you", "your",
}


def _is_routing_correct(c: RoutingCase, t: Dict[str, Any]) -> bool:
    pred = t.get("predicted", {})
    if pred.get("decision") != c.expected.decision:
        return False
    if c.expected.decision == "auto_rag":
        return pred.get("intent") in c.expected.acceptable_intents
    if c.expected.decision == "escalate":
        return pred.get("queue") == c.expected.queue
    return False


def _classify_error_type(
    c: RoutingCase,
    t: Dict[str, Any],
    valid_intents: Sequence[str],
) -> str:
    """Classify the primary error type adhering strictly to spec precedence.

    Precedence: error > degraded > invalid_label > missed_escalation > wrong_queue > false_escalation > wrong_intent.
    """
    pred = t.get("predicted", {})
    p_tier = str(pred.get("tier", ""))
    p_intent = str(pred.get("intent", ""))
    p_dec = str(pred.get("decision", ""))
    p_queue = str(pred.get("queue", ""))
    p_escalate = bool(pred.get("escalate", False))
    p_degraded = bool(pred.get("degraded", False))

    # 1. error
    if t.get("error") is not None or p_tier == "error":
        return "error"

    # 2. degraded
    if p_degraded or p_tier == "fallback_degraded":
        return "degraded"

    # 3. invalid_label
    if p_intent not in valid_intents:
        return "invalid_label"

    # 4. missed_escalation (expected escalate, predicted auto_rag/not escalate)
    if c.expected.escalate and (not p_escalate or p_dec == "auto_rag"):
        return "missed_escalation"

    # 5. wrong_queue (escalate case where queue doesn't match)
    if c.expected.escalate and p_queue != c.expected.queue:
        return "wrong_queue"

    # 6. false_escalation (expected auto_rag, predicted escalate)
    if not c.expected.escalate and (p_escalate or p_dec == "escalate"):
        return "false_escalation"

    # 7. wrong_intent (intent not in acceptable intents)
    if p_intent not in c.expected.acceptable_intents:
        return "wrong_intent"

    return "wrong_intent"


def _tokenize(text: str) -> List[str]:
    """Tokenize and remove basic stopwords."""
    tokens = _RE_TOKEN.findall(text.lower())
    return [tok for tok in tokens if tok not in _STOPWORDS]


def _build_tfidf_vectors(texts: Sequence[str]) -> List[Dict[str, float]]:
    """Compute normalized TF-IDF sparse vectors for a sequence of texts."""
    n_docs = len(texts)
    if n_docs == 0:
        return []

    doc_tfs: List[Dict[str, float]] = []
    df: Dict[str, int] = {}

    for txt in texts:
        tokens = _tokenize(txt)
        tf: Dict[str, float] = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0.0) + 1.0
        doc_tfs.append(tf)
        for tok in set(tokens):
            df[tok] = df.get(tok, 0) + 1

    vectors: List[Dict[str, float]] = []
    for tf in doc_tfs:
        vec: Dict[str, float] = {}
        sq_sum = 0.0
        for tok, count in tf.items():
            # Smooth IDF
            idf = math.log((1.0 + n_docs) / (1.0 + df.get(tok, 1))) + 1.0
            val = count * idf
            vec[tok] = val
            sq_sum += val * val

        norm = math.sqrt(sq_sum)
        if norm > 0.0:
            for tok in vec:
                vec[tok] /= norm
        vectors.append(vec)

    return vectors


def _cosine_similarity(v1: Dict[str, float], v2: Dict[str, float]) -> float:
    """Compute cosine similarity between two unit-normalized sparse vectors."""
    dot = 0.0
    # Iterate over the smaller dict
    small, big = (v1, v2) if len(v1) <= len(v2) else (v2, v1)
    for tok, val in small.items():
        if tok in big:
            dot += val * big[tok]
    return max(0.0, min(1.0, dot))


def _agglomerate_cluster_indices(
    vectors: Sequence[Dict[str, float]],
    ticket_ids: Sequence[str],
    similarity_threshold: float,
) -> List[List[int]]:
    """Average-link agglomerative clustering over vector indices with deterministic tie-breaks."""
    n = len(vectors)
    if n <= 1:
        return [[i] for i in range(n)]

    # Precompute pairwise similarities
    sim_matrix: List[List[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        sim_matrix[i][i] = 1.0
        for j in range(i + 1, n):
            s = _cosine_similarity(vectors[i], vectors[j])
            sim_matrix[i][j] = s
            sim_matrix[j][i] = s

    # Initialize clusters as singletons
    clusters: List[List[int]] = [[i] for i in range(n)]

    while len(clusters) > 1:
        best_sim = -1.0
        best_pair: Optional[Tuple[int, int]] = None
        best_tie_key: Tuple[str, str] = ("", "")

        for a_idx in range(len(clusters)):
            for b_idx in range(a_idx + 1, len(clusters)):
                c_a = clusters[a_idx]
                c_b = clusters[b_idx]

                # Average link similarity
                tot_sim = sum(sim_matrix[i][j] for i in c_a for j in c_b)
                avg_sim = tot_sim / (len(c_a) * len(c_b))

                if avg_sim < similarity_threshold:
                    continue

                min_tid_a = min(ticket_ids[i] for i in c_a)
                min_tid_b = min(ticket_ids[j] for j in c_b)
                tie_key = (min(min_tid_a, min_tid_b), max(min_tid_a, min_tid_b))

                if avg_sim > best_sim:
                    best_sim = avg_sim
                    best_pair = (a_idx, b_idx)
                    best_tie_key = tie_key
                elif abs(avg_sim - best_sim) < 1e-9:
                    if tie_key < best_tie_key:
                        best_sim = avg_sim
                        best_pair = (a_idx, b_idx)
                        best_tie_key = tie_key

        if best_pair is None or best_sim < similarity_threshold:
            break

        idx_1, idx_2 = best_pair
        merged = clusters[idx_1] + clusters[idx_2]
        # Remove larger index first to keep indices stable
        clusters.pop(idx_2)
        clusters.pop(idx_1)
        clusters.append(merged)

    return clusters


def cluster_failures(
    traces: Sequence[Dict[str, Any]],
    cases: Sequence[RoutingCase],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Group and cluster incorrect routing cases by taxonomy and semantic similarity.

    Args:
        traces: Sequence of router trace dictionaries.
        cases: Sequence of RoutingCase instances.
        cfg: Optional configuration dictionary.

    Returns:
        List of cluster dictionaries ranked by severity score descending.
    """
    cfg = cfg or {}
    sim_threshold = float(cfg.get("cluster_similarity_threshold", 0.35))

    tax = load_taxonomy()
    valid_intents = list(tax.intents)

    case_map = {c.ticket_id: c for c in cases if c.label_status != "rejected"}
    trace_map = {t["ticket_id"]: t for t in traces if "ticket_id" in t}

    # 1. Filter incorrect cases
    incorrect_items: List[Tuple[RoutingCase, Dict[str, Any]]] = []
    for tid, c in case_map.items():
        if tid in trace_map:
            t = trace_map[tid]
            if not _is_routing_correct(c, t):
                incorrect_items.append((c, t))

    if not incorrect_items:
        return []

    # 2. Partition into group keys: (error_type, expected->predicted, deciding_tier, primary_scenario)
    groups: Dict[Tuple[str, str, str, str], List[Tuple[RoutingCase, Dict[str, Any]]]] = {}
    for c, t in incorrect_items:
        err_type = _classify_error_type(c, t, valid_intents)
        p_intent = str(t.get("predicted", {}).get("intent", "none"))
        transition = f"{c.expected.intent}->{p_intent}"
        dec_tier = str(t.get("predicted", {}).get("tier", "unknown"))
        primary_scenario = c.scenarios[0] if c.scenarios else "normal"

        g_key = (err_type, transition, dec_tier, primary_scenario)
        groups.setdefault(g_key, []).append((c, t))

    raw_clusters: List[Dict[str, Any]] = []

    # 3. Within each group key, perform TF-IDF cosine agglomeration
    for (err_type, transition, dec_tier, primary_scenario), items in sorted(groups.items()):
        texts = [f"{c.subject or ''} {c.description}" for c, _ in items]
        tids = [c.ticket_id for c, _ in items]

        vectors = _build_tfidf_vectors(texts)
        clustered_indices = _agglomerate_cluster_indices(vectors, tids, sim_threshold)

        for cluster_sub_idx in clustered_indices:
            cluster_items = [items[idx] for idx in cluster_sub_idx]
            cluster_vectors = [vectors[idx] for idx in cluster_sub_idx]
            cluster_size = len(cluster_items)

            # Severity calculation
            sev_weight = SEVERITY_WEIGHTS.get(err_type, 1)
            score = cluster_size * sev_weight

            # Compute signature: top 5 TF-IDF terms for cluster
            cluster_term_weights: Dict[str, float] = {}
            for v in cluster_vectors:
                for tok, w in v.items():
                    cluster_term_weights[tok] = cluster_term_weights.get(tok, 0.0) + w

            sorted_terms = sorted(
                cluster_term_weights.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )
            top_terms = [tok for tok, _ in sorted_terms[:5]]

            # Medoid selection: member maximizing average cosine similarity to all cluster items
            m_scores: List[Tuple[float, str, int]] = []
            for i_local, idx_a in enumerate(cluster_sub_idx):
                tot_sim = sum(
                    _cosine_similarity(cluster_vectors[i_local], cluster_vectors[j_local])
                    for j_local in range(cluster_size)
                )
                avg_sim = tot_sim / cluster_size
                m_scores.append((avg_sim, tids[idx_a], i_local))

            # Best medoid has highest avg_sim, min ticket_id
            m_scores.sort(key=lambda item: (-item[0], item[1]))
            medoid_local_idx = m_scores[0][2]
            medoid_vector = cluster_vectors[medoid_local_idx]

            # Rank representatives: medoid first, then closest to medoid
            rep_scores: List[Tuple[float, str, int]] = []
            for i_local, idx_a in enumerate(cluster_sub_idx):
                sim_to_medoid = _cosine_similarity(cluster_vectors[i_local], medoid_vector)
                rep_scores.append((sim_to_medoid, tids[idx_a], i_local))

            # Medoid has sim 1.0 and is selected first; tie break by ticket_id
            rep_scores.sort(key=lambda item: (-item[0], item[1]))
            selected_local_indices = [item[2] for item in rep_scores[:5]]

            representatives: List[Dict[str, Any]] = []
            for loc_idx in selected_local_indices:
                c_rep, t_rep = cluster_items[loc_idx]
                representatives.append({
                    "ticket_id": c_rep.ticket_id,
                    "subject": c_rep.subject,
                    "description": c_rep.description[:200],
                    "expected": c_rep.expected.to_dict(),
                    "predicted": t_rep.get("predicted"),
                })

            # Tier details summary
            t1_probs = [
                float(t.get("decision_raw", {}).get("details", {}).get("intent_prob", 0.0))
                for _, t in cluster_items
                if "intent_prob" in t.get("decision_raw", {}).get("details", {})
            ]
            avg_intent_prob = (sum(t1_probs) / len(t1_probs)) if t1_probs else None

            raw_clusters.append({
                "error_type": err_type,
                "transition": transition,
                "deciding_tier": dec_tier,
                "primary_scenario": primary_scenario,
                "size": cluster_size,
                "severity_weight": sev_weight,
                "score": score,
                "signature": top_terms,
                "avg_tier1_intent_prob": round(avg_intent_prob, 4) if avg_intent_prob else None,
                "representatives": representatives,
            })

    # 4. Globally rank clusters by score desc, size desc, transition asc
    raw_clusters.sort(
        key=lambda cl: (-cl["score"], -cl["size"], cl["error_type"], cl["transition"])
    )

    # Assign 1-based cluster_id
    for idx, cl in enumerate(raw_clusters, 1):
        cl["cluster_id"] = idx

    return raw_clusters
