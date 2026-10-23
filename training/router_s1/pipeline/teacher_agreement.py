#!/usr/bin/env python3
"""teacher_agreement.py — how consistent is the teacher, and how often does Laya reproduce it?

Inputs: labels.jsonl (all teacher votes), test.jsonl (dataset split; `id` = thread_id), and
pred_test.jsonl (evaluate.py --dump, same row order as test.jsonl).
Reports, per question:
  * teacher self-consistency on all labels: unanimous rate (3/3), pairwise vote agreement
  * Laya vs teacher majority on test: overall, on unanimous cases, on split cases
  * Laya vs a single teacher vote, compared with vote-vs-vote agreement (the teacher's own ceiling)

  python teacher_agreement.py labels.jsonl test.jsonl pred_test.jsonl
"""
import itertools, json, sys
from collections import Counter

Q = ("escalate", "sentiment", "urgency", "intent")


def vote_val(v, q):
    return str(v[q]).lower() if q != "urgency" else str(int(v[q]))


def pred_val(p, q):
    if q == "escalate":
        return "true" if p["escalate"].get("true", 0.0) >= 0.5 else "false"
    return max(p[q], key=p[q].get)


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 3 or args[0] in ("-h", "--help"):
        print("Usage: python teacher_agreement.py labels.jsonl test.jsonl pred_test.jsonl")
        return 0

    labels = {}
    for l in open(args[0]):
        r = json.loads(l)
        labels[str(r["thread_id"])] = [v for v in r["votes"] if "error" not in v]
    test = [json.loads(l) for l in open(args[1])]
    pred = [json.loads(l) for l in open(args[2])]
    assert len(test) == len(pred), (len(test), len(pred))

    print("TEACHER SELF-CONSISTENCY (all %d labeled messages, 3 votes each)" % len(labels))
    print(f"{'question':10s} {'unanimous':>9s} {'pairwise':>9s}")
    for q in Q:
        un = pw_hit = pw_n = 0
        for votes in labels.values():
            vals = [vote_val(v, q) for v in votes]
            un += len(set(vals)) == 1
            for a, b in itertools.combinations(vals, 2):
                pw_hit += a == b; pw_n += 1
        print(f"{q:10s} {un / len(labels):9.3f} {pw_hit / pw_n:9.3f}")

    print("\nLAYA v2 vs TEACHER on test (%d messages)" % len(test))
    print(f"{'question':10s} {'vs majority':>11s} {'unanimous':>16s} {'split':>16s} {'laya~vote':>10s} {'vote~vote':>10s}")
    for q in Q:
        rows = []
        for t, p in zip(test, pred):
            votes = [vote_val(v, q) for v in labels[t["id"]]]
            maj, cnt = Counter(votes).most_common(1)[0]
            y = pred_val(p["p"], q)
            rows.append((y, maj, len(set(votes)) == 1, votes))
        acc = sum(y == m for y, m, _, _ in rows) / len(rows)
        un = [r for r in rows if r[2]]; sp = [r for r in rows if not r[2]]
        acc_un = sum(y == m for y, m, _, _ in un) / max(1, len(un)); acc_sp = sum(y == m for y, m, _, _ in sp) / max(1, len(sp))
        lv = sum(sum(y == v for v in vs) / len(vs) for y, _, _, vs in rows) / len(rows)
        vv_hit = vv_n = 0
        for _, _, _, vs in rows:
            for a, b in itertools.combinations(vs, 2): vv_hit += a == b; vv_n += 1
        print(f"{q:10s} {acc:11.3f} {acc_un:9.3f} (n={len(un):3d}) {acc_sp:9.3f} (n={len(sp):3d}) {lv:10.3f} {vv_hit / vv_n:10.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
