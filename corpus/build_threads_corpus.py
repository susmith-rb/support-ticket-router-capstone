#!/usr/bin/env python3
"""build_threads_corpus.py — `resolved-threads` documents for Agent Search (secondary evidence, not citable).

One document per AmazonHelp thread that has at least one agent reply (thread-aware: the whole
conversation stays together; tweets are short, so threads fit well under 512 tokens and are
truncated at a turn boundary if not). Teacher labels are attached as metadata where available.
Output: Discovery Engine `document` JSONL with structData (structured data store).

  python build_threads_corpus.py --threads threads.jsonl --labels labels.jsonl --out resolved_threads_import.jsonl
"""
import argparse
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-chars", type=int, default=2000)
    a = ap.parse_args()
    lab = {}
    for l in open(a.labels):
        r = json.loads(l)
        g = r["gold"]
        lab[str(r["thread_id"])] = {
            "intent": max(g["intent"]["probabilities"], key=g["intent"]["probabilities"].get),
            "sentiment": max(g["sentiment"]["probabilities"], key=g["sentiment"]["probabilities"].get),
            "topic": next((v.get("topic") for v in r["votes"] if v.get("topic")), ""),
        }
    n = kept = 0
    with open(a.out, "w") as fh:
        for l in open(a.threads):
            n += 1
            t = json.loads(l)
            if not any(x["role"] == "agent" for x in t["turns"]):
                continue
            lines, total = [], 0
            for x in t["turns"]:
                s = f"{'Customer' if x['role'] == 'customer' else 'Agent'}: {x['text']}"
                if total + len(s) > a.max_chars and lines:
                    break
                lines.append(s)
                total += len(s) + 1
            tid = str(t["thread_id"])
            meta = lab.get(tid, {})
            fh.write(json.dumps({"id": f"thread-{tid}", "structData": {
                "thread_id": tid, "text": "\n".join(lines), "n_turns": len(t["turns"]), "first_customer_message": t["turns"][0]["text"],
                "intent": meta.get("intent", "unlabeled"), "sentiment": meta.get("sentiment", "unlabeled"), "topic": meta.get("topic", ""),
                "source": "kaggle-customer-support-on-twitter/AmazonHelp", "citable": False}}) + "\n")
            kept += 1
    print(f"threads read {n}, documents written {kept} -> {a.out}")


if __name__ == "__main__":
    main()
