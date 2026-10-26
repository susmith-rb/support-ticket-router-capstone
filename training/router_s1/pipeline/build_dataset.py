#!/usr/bin/env python3
"""build_dataset.py — teacher labels -> laya-train JSONL splits (train / val / test).

Each output row is a Laya training case:
  {"id", "state": <redacted text>, "questions": QUESTIONS, "gold": {qid: {"probabilities": {...}}}}
Soft `gold` targets keep the teacher's vote shares (calibration signal); `expected` holds the
argmax for laya-evals compatibility. Splits are stratified by (sentiment, intent) argmax.
The `test` split is teacher-labeled, NOT the human-verified golden set; promote it to golden only
after two humans review it (see README).

  python build_dataset.py --labels data/labels/labels.jsonl --out data/dataset/v1
"""
import argparse
import collections
import json
import pathlib
import random
from training.router_s1.pipeline.router_schema import QUESTIONS

def argmax(p): return max(p, key=p.get)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="data/labels/labels.jsonl")
    ap.add_argument("--out", default="data/dataset/v1")
    ap.add_argument("--val", type=float, default=0.1)
    ap.add_argument("--test", type=float, default=0.1)
    ap.add_argument("--max-chars", type=int, default=1800)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.labels)]
    seen, cases = set(), []
    for r in rows:
        key = r["text"].strip().lower()
        if key in seen or not key:
            continue
        seen.add(key)
        g = r["gold"]
        expected = {
            "escalate": g["escalate"]["probabilities"]["true"] >= 0.5,
            "sentiment": argmax(g["sentiment"]["probabilities"]),
            "urgency": int(argmax(g["urgency"]["probabilities"])),
            "intent": argmax(g["intent"]["probabilities"]),
        }
        cases.append({"id": str(r["thread_id"]), "state": r["text"][: a.max_chars],
                      "questions": QUESTIONS, "gold": g, "expected": expected})

    strata = collections.defaultdict(list)
    for c in cases:
        strata[(c["expected"]["sentiment"], c["expected"]["intent"])].append(c)
    rng = random.Random(a.seed)
    split = {"train": [], "val": [], "test": []}
    for _, group in sorted(strata.items()):
        rng.shuffle(group)
        n = len(group); n_te = round(n * a.test); n_va = round(n * a.val)
        split["test"] += group[:n_te]; split["val"] += group[n_te:n_te + n_va]; split["train"] += group[n_te + n_va:]

    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for name, items in split.items():
        rng.shuffle(items)
        with open(out / f"{name}.jsonl", "w") as fh:
            for c in items:
                fh.write(json.dumps(c) + "\n")
    stats = {name: {
                "n": len(items),
                "escalate_rate": round(sum(c["expected"]["escalate"] for c in items) / max(1, len(items)), 3),
                "sentiment": collections.Counter(c["expected"]["sentiment"] for c in items),
                "intent": collections.Counter(c["expected"]["intent"] for c in items)}
             for name, items in split.items()}
    stats["source_labels"] = len(rows); stats["unique_cases"] = len(cases)
    json.dump(stats, open(out / "stats.json", "w"), indent=1, default=dict)
    print(json.dumps(stats, indent=1, default=dict))

if __name__ == "__main__":
    main()
