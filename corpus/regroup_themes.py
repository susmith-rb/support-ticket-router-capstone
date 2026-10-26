#!/usr/bin/env python3
"""regroup_themes.py — regroup FAQ themes for RAG corpus v2.

Pipeline:
  1. Merges overlapping order-status themes into unified articles:
     - late general delivery / late prime delivery / general order not arrived / order not shipped
       -> faq-order-status-late-or-not-arrived-order
     - missing-delivered / stolen -> faq-order-status-missing-or-stolen-delivered-package
  2. Splits/re-clusters the `other` intent family into coherent sub-themes (using an injectable
     LLM clusterer with preview fallback) and drops pure praise/noise.
  3. Builds `themes_v2.json` and a complete `gold_map_v1_v2.json` where every v1 theme either maps
     to a v2 doc id or is listed under "dropped" with an explicit reason.
  4. Validates invariants:
     - Merge application correctness
     - Gold-map completeness
     - No thread assigned to multiple v2 themes

Usage:
  python rag/regroup_themes.py --themes rag/results/themes_v1.json \
      --labels scratch/rag/labels.jsonl --threads scratch/rag/threads.jsonl \
      --out-themes scratch/rag/faq_v2/themes_v2.json \
      --out-gold-map scratch/rag/faq_v2/gold_map_v1_v2.json \
      --project susmith-fde-ticket-router
"""

import argparse
import collections
import json
import pathlib
import re

from ticket_router.config.models import model_id

try:
    from corpus.build_faq_corpus import THEME_SCHEMA, Tok, gemini
except ImportError:
    # Fallback if imported in test environment without full dependencies
    THEME_SCHEMA = {"type": "object", "required": ["themes"]}
    Tok = None
    gemini = None


def doc_id_for_theme(theme_dict_or_intent, theme_id=None):
    """Derive canonical doc_id `faq-<intent>-<slug>` from theme dict or intent + theme_id."""
    if isinstance(theme_dict_or_intent, dict):
        intent = theme_dict_or_intent.get("intent_family", "")
        tid = theme_dict_or_intent.get("theme_id", "")
    else:
        intent = theme_dict_or_intent
        tid = theme_id
    raw = f"faq-{intent}-{tid}".lower().replace("_", "-")
    return re.sub(r"[^a-z0-9-]", "-", raw)[:120]


ORDER_STATUS_MERGES = [
    {
        "target_theme_id": "late_or_not_arrived_order",
        "intent_family": "order_status",
        "title": "Late or Not-Arrived Orders",
        "description": "Inquiries, delays, and tracking issues regarding orders that are late, delayed, not yet shipped or dispatched, or have not arrived by the estimated delivery date.",
        "source_doc_ids": [
            "faq-order-status-late-general-delivery",
            "faq-order-status-late-prime-delivery",
            "faq-order-status-general-order-not-arrived",
            "faq-order-status-order-not-shipped",
        ],
    },
    {
        "target_theme_id": "missing_or_stolen_delivered_package",
        "intent_family": "order_status",
        "title": "Missing or Stolen Package Marked Delivered",
        "description": "Customer issues and assistance for packages marked as delivered that cannot be found, are missing from delivery locations, or are suspected stolen.",
        "source_doc_ids": [
            "faq-order-status-missing-delivered-package",
            "faq-order-status-stolen-packages",
        ],
    },
]

DEFAULT_DROPPED = {
    "faq-order-status-positive-feedback": "Non-actionable positive feedback and early delivery compliments",
    "faq-other-customer-service-positive-feedback": "Non-actionable positive customer service feedback",
    "faq-other-delivery-positive-feedback": "Non-actionable positive delivery feedback",
    "faq-other-social-media-and-sharing": "Non-actionable social media chatter, jokes, and brand mentions",
    "faq-other-general-complaints-and-feedback": "Non-actionable unstructured general dissatisfaction and complaints",
}


def apply_merges(themes, merges=None):
    """Merge overlapping themes according to merge specifications.

    Combines topics (deduped, stable order) and thread_ids (deduped, stable order).
    Removes source themes and adds merged target themes.
    Returns (updated_themes, merge_map: {source_doc_id: target_doc_id}).
    """
    if merges is None:
        merges = ORDER_STATUS_MERGES

    src_to_merge = {}
    for m in merges:
        for s in m["source_doc_ids"]:
            src_to_merge[s] = m

    merge_map = {}
    merged_themes_dict = {}
    for m in merges:
        target_doc_id = doc_id_for_theme(m["intent_family"], m["target_theme_id"])
        for s in m["source_doc_ids"]:
            merge_map[s] = target_doc_id
        merged_themes_dict[target_doc_id] = {
            "theme_id": m["target_theme_id"],
            "title": m["title"],
            "description": m["description"],
            "intent_family": m["intent_family"],
            "topics": [],
            "thread_ids": [],
        }

    remaining_themes = []
    seen_threads_for_merged = collections.defaultdict(set)
    seen_topics_for_merged = collections.defaultdict(set)

    for th in themes:
        did = doc_id_for_theme(th)
        if did in src_to_merge:
            m = src_to_merge[did]
            target_doc_id = doc_id_for_theme(m["intent_family"], m["target_theme_id"])
            target = merged_themes_dict[target_doc_id]
            for top in th.get("topics", []):
                top_clean = top.strip().lower()
                if top_clean not in seen_topics_for_merged[target_doc_id]:
                    seen_topics_for_merged[target_doc_id].add(top_clean)
                    target["topics"].append(top)
            for tid in th.get("thread_ids", []):
                tid_str = str(tid)
                if tid_str not in seen_threads_for_merged[target_doc_id]:
                    seen_threads_for_merged[target_doc_id].add(tid_str)
                    target["thread_ids"].append(tid_str)
        else:
            remaining_themes.append(dict(th))

    for m in merges:
        target_doc_id = doc_id_for_theme(m["intent_family"], m["target_theme_id"])
        target = merged_themes_dict[target_doc_id]
        if target["thread_ids"]:
            remaining_themes.append(target)

    return remaining_themes, merge_map


def reassign_other_threads(clustered_themes, other_msgs):
    """Assign threads to clustered themes, ensuring no thread is assigned to multiple themes."""
    t2theme = {}
    for th in clustered_themes:
        for t in th.get("topics", []):
            norm_t = t.strip().lower()
            if norm_t not in t2theme:
                t2theme[norm_t] = th["theme_id"]

    assigned_threads = set()
    result_themes = []
    for th in clustered_themes:
        tid_list = []
        for tid, t in other_msgs:
            tid_str = str(tid)
            if tid_str not in assigned_threads and t2theme.get(t) == th["theme_id"]:
                tid_list.append(tid_str)
                assigned_threads.add(tid_str)
        th_copy = dict(th)
        th_copy["intent_family"] = "other"
        th_copy["thread_ids"] = tid_list
        result_themes.append(th_copy)

    return result_themes


def cluster_other_topics(other_msgs, cluster_fn=None, min_theme_size=6, target_k="10-18"):
    """Cluster 'other' messages into coherent sub-themes.

    cluster_fn: callable(prompt, schema) -> (dict_result, usage_dict).
    If cluster_fn is None, caller must provide a valid LLM clusterer or use offline fallback.
    """
    if not other_msgs:
        return [], {}

    cnt = collections.Counter(t for _, t in other_msgs)
    topic_list = "\n".join(f"{n}\t{t}" for t, n in cnt.most_common(700))
    prompt = (
        f"These are short issue topics (count<TAB>topic) from customer-support messages to an online retailer, "
        f"all labeled intent='other'. Group the answerable customer support topics into {target_k} coherent FAQ themes, "
        f"each answerable by ONE help-center article for Global Retail Hub (including devices like Kindle/Echo/Fire, "
        f"Prime Video and digital streaming, app and website technical issues, scams and phishing, product availability, "
        f"pricing and promotions). Exclude or drop pure praise, social media banter, and non-actionable complaints. "
        f"Every included topic must appear in exactly one theme; copy topic strings exactly. theme_id: short snake_case.\n\n{topic_list}"
    )

    if cluster_fn is None:
        raise ValueError("cluster_fn is required for clustering; provide an LLM function or use offline fallback")

    res, usage = cluster_fn(prompt, THEME_SCHEMA)
    raw_themes = res.get("themes", [])
    assigned = reassign_other_threads(raw_themes, other_msgs)
    viable_themes = [th for th in assigned if len(th.get("thread_ids", [])) >= min_theme_size]
    return viable_themes, usage


def build_gold_map(v1_themes, v2_themes, merge_map, dropped_dict=None, other_map=None):
    """Construct a complete gold map mapping every v1 theme to a v2 doc id or explicit dropped reason.

    Returns dict containing:
      "map": {v1_doc_id: v2_doc_id},
      "dropped": {v1_doc_id: reason},
      and top-level keys for all mapped v1_doc_id -> v2_doc_id.
    """
    if dropped_dict is None:
        dropped_dict = DEFAULT_DROPPED
    if other_map is None:
        other_map = {}

    v2_doc_ids = {doc_id_for_theme(th) for th in v2_themes}
    mapping = {}
    dropped = dict(dropped_dict)

    for th in v1_themes:
        v1_id = doc_id_for_theme(th)
        if v1_id in dropped:
            continue
        elif v1_id in merge_map:
            tgt = merge_map[v1_id]
            if tgt not in v2_doc_ids:
                raise ValueError(f"Merged target '{tgt}' for '{v1_id}' does not exist in v2 doc ids")
            mapping[v1_id] = tgt
        elif v1_id in other_map:
            tgt = other_map[v1_id]
            if tgt not in v2_doc_ids:
                raise ValueError(f"Other mapping target '{tgt}' for '{v1_id}' does not exist in v2 doc ids")
            mapping[v1_id] = tgt
        elif v1_id in v2_doc_ids:
            mapping[v1_id] = v1_id
        else:
            raise ValueError(f"v1 theme '{v1_id}' is neither mapped, dropped, nor found in v2 themes")

    gold_map = {
        "map": mapping,
        "dropped": dropped,
        **mapping,
    }
    return gold_map


def validate_v2(v1_themes, v2_themes, gold_map):
    """Validate invariants on v2 themes and gold map."""
    # 1. No thread assigned to two v2 themes
    seen_threads = {}
    for th in v2_themes:
        th_id = th.get("theme_id", "unknown")
        for tid in th.get("thread_ids", []):
            tid_str = str(tid)
            if tid_str in seen_threads:
                raise ValueError(
                    f"Invariant violated: thread '{tid_str}' is assigned to multiple v2 themes: "
                    f"'{seen_threads[tid_str]}' and '{th_id}'"
                )
            seen_threads[tid_str] = th_id

    # 2. Gold map completeness
    v1_doc_ids = {doc_id_for_theme(th) for th in v1_themes}
    v2_doc_ids = {doc_id_for_theme(th) for th in v2_themes}
    dropped = gold_map.get("dropped", {})

    for did in v1_doc_ids:
        if did in dropped:
            reason = dropped[did]
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"Dropped theme '{did}' has an empty or invalid reason: {reason!r}")
        else:
            tgt = gold_map.get(did)
            if not tgt:
                raise ValueError(f"Gold map missing entry for v1 doc '{did}'")
            if tgt not in v2_doc_ids:
                raise ValueError(f"Gold map entry '{did}' -> '{tgt}', but '{tgt}' not in v2 themes")

    # 3. Check internal "map" key matches
    internal_map = gold_map.get("map", {})
    for src, tgt in internal_map.items():
        if tgt not in v2_doc_ids:
            raise ValueError(f"Map entry '{src}' -> '{tgt}' points to non-existent v2 doc")


def regroup(v1_themes, labels=None, cluster_fn=None, min_theme_size=6, dropped_dict=None, decisions=None):
    """Full regrouping pipeline: merges order-status, re-clusters or cleans other, builds gold map.

    Returns (v2_themes, gold_map, usage_dict).
    """
    if decisions is not None:
        merges = decisions.get("merges", ORDER_STATUS_MERGES)
        dropped_dict = decisions.get("dropped", DEFAULT_DROPPED)
        new_themes = decisions.get("new_themes", [])
        replaced_ids = set(decisions.get("replaced_theme_ids", []))

        # 1. Apply order-status merges
        themes_after_merge, merge_map = apply_merges(v1_themes, merges)

        # 2. Kept themes
        kept_themes = []
        for th in themes_after_merge:
            did = doc_id_for_theme(th)
            if did in dropped_dict:
                continue
            if th.get("theme_id") in replaced_ids:
                continue
            kept_themes.append(dict(th))

        v2_themes = kept_themes + new_themes

        # 3. Gold map mapping for replaced / new themes
        other_map = dict(decisions.get("other_map", {}))
        for rep_id in replaced_ids:
            old_did = f"faq-other-{rep_id}".replace("_", "-")
            if old_did not in other_map and old_did in {doc_id_for_theme(t) for t in new_themes}:
                other_map[old_did] = old_did

        gold_map = build_gold_map(v1_themes, v2_themes, merge_map, dropped_dict, other_map=other_map)
        validate_v2(v1_themes, v2_themes, gold_map)
        return v2_themes, gold_map, {}

    if dropped_dict is None:
        dropped_dict = DEFAULT_DROPPED

    # 1. Apply order-status merges
    themes_after_merge, merge_map = apply_merges(v1_themes, ORDER_STATUS_MERGES)

    # 2. Handle 'other' intent family
    # Separate non-other themes from other themes, filtering out dropped themes
    non_other_themes = [
        th for th in themes_after_merge
        if th.get("intent_family") != "other" and doc_id_for_theme(th) not in dropped_dict
    ]
    v1_other_themes = [th for th in v1_themes if th.get("intent_family") == "other"]

    usage = collections.Counter()
    other_map = {}

    if cluster_fn is not None and labels is not None:
        # Re-cluster other from labels
        other_msgs = []
        for l in labels:
            r = json.loads(l) if isinstance(l, str) else l
            topics = [v.get("topic") for v in r.get("votes", []) if v.get("topic")]
            if not topics:
                continue
            probs = r.get("gold", {}).get("intent", {}).get("probabilities", {})
            if probs and max(probs, key=probs.get) == "other":
                other_msgs.append((str(r["thread_id"]), topics[0].strip().lower()))

        v2_other_themes, u = cluster_other_topics(other_msgs, cluster_fn=cluster_fn, min_theme_size=min_theme_size)
        for k, v in u.items():
            if isinstance(v, int):
                usage[k] += v

        # Map v1 other themes to v2 other themes
        v2_other_by_id = {th["theme_id"]: th for th in v2_other_themes}
        for th in v1_other_themes:
            did = doc_id_for_theme(th)
            if did in dropped_dict:
                continue
            tid = th["theme_id"]
            if tid in v2_other_by_id:
                other_map[did] = doc_id_for_theme(v2_other_by_id[tid])
            elif "device" in tid or "hardware" in tid:
                # Map hardware/device themes to first matching device theme
                dev_target = next(
                    (doc_id_for_theme(t) for t in v2_other_themes if "device" in t["theme_id"] or "hardware" in t["theme_id"]),
                    None
                )
                if dev_target:
                    other_map[did] = dev_target
    else:
        # Offline/deterministic fallback: keep answerable v1 other themes (filtering dropped)
        v2_other_themes = []
        for th in v1_other_themes:
            did = doc_id_for_theme(th)
            if did not in dropped_dict:
                v2_other_themes.append(dict(th))

    v2_themes = non_other_themes + v2_other_themes

    # 3. Build gold map and validate
    gold_map = build_gold_map(v1_themes, v2_themes, merge_map, dropped_dict, other_map=other_map)
    validate_v2(v1_themes, v2_themes, gold_map)

    return v2_themes, gold_map, dict(usage)


def main():
    ap = argparse.ArgumentParser(description="Regroup themes for RAG corpus v2")
    ap.add_argument("--themes", default="rag/results/themes_v1.json", help="Path to themes_v1.json")
    ap.add_argument("--labels", default=None, help="Path to labels.jsonl")
    ap.add_argument("--threads", default=None, help="Path to threads.jsonl")
    ap.add_argument("--out-themes", required=True, help="Output path for themes_v2.json")
    ap.add_argument("--out-gold-map", required=True, help="Output path for gold_map_v1_v2.json")
    ap.add_argument("--decisions", default=None, help="Path to precomputed agent decisions JSON")
    ap.add_argument("--project", default="susmith-fde-ticket-router")
    ap.add_argument("--min-theme-size", type=int, default=6)
    ap.add_argument("--offline", action="store_true", help="Run without calling remote LLM API")
    a = ap.parse_args()

    v1_themes = json.load(open(a.themes))
    decisions = json.load(open(a.decisions)) if a.decisions and pathlib.Path(a.decisions).exists() else None
    labels = open(a.labels).readlines() if a.labels and pathlib.Path(a.labels).exists() else None

    cluster_fn = None
    if decisions is None and not a.offline and labels is not None and gemini is not None and Tok is not None:
        tok = Tok()
        def _live_cluster(prompt, schema):
            return gemini(tok, a.project, prompt, schema, model=model_id("corpus_author"))
        cluster_fn = _live_cluster

    v2_themes, gold_map, usage = regroup(
        v1_themes,
        labels=labels,
        cluster_fn=cluster_fn,
        min_theme_size=a.min_theme_size,
        decisions=decisions,
    )

    out_th = pathlib.Path(a.out_themes)
    out_th.parent.mkdir(parents=True, exist_ok=True)
    with open(out_th, "w") as f:
        json.dump(v2_themes, f, indent=1)

    out_gm = pathlib.Path(a.out_gold_map)
    out_gm.parent.mkdir(parents=True, exist_ok=True)
    with open(out_gm, "w") as f:
        json.dump(gold_map, f, indent=1)

    print(f"Regroup complete: v1 themes {len(v1_themes)} -> v2 themes {len(v2_themes)}")
    print(f"Gold map: {len(gold_map.get('map', {}))} mapped, {len(gold_map.get('dropped', {}))} dropped")
    if usage:
        print(f"Token usage: {usage}")


if __name__ == "__main__":
    main()
