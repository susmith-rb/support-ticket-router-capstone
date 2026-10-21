#!/usr/bin/env python3
"""evaluate.py — score a Laya checkpoint on a laya-train JSONL split, router-style.

Runs in the SERVING venv (laya 0.3.20, what the Model Garden container pins), so it also proves
the checkpoint is servable. Reports per-question accuracy vs teacher argmax, escalation recall /
false-escalation trade-off across thresholds (the tau_esc sweep), intent macro-F1, ECE, latency.

  python evaluate.py --ckpt runs/v1/ckpt --data data/dataset/v1/test.jsonl --out runs/v1/eval_test.json
  python evaluate.py --ckpt english --data ...      # zero-shot baseline (built-in name)
"""
import argparse, json, statistics, time
import laya
from router_schema import QUESTIONS, SENTIMENTS, INTENTS

def ece(confs, correct, bins=15):
    tot, err = len(confs), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(confs) if lo < c <= hi or (b == 0 and c == 0)]
        if idx:
            err += len(idx) / tot * abs(sum(correct[i] for i in idx) / len(idx) - sum(confs[i] for i in idx) / len(idx))
    return err

def macro_f1(y, p, labels):
    f1s = []
    for l in labels:
        tp = sum(a == l and b == l for a, b in zip(y, p)); fp = sum(a != l and b == l for a, b in zip(y, p))
        fn = sum(a == l and b != l for a, b in zip(y, p))
        if tp + fp + fn == 0: continue
        prec = tp / (tp + fp) if tp + fp else 0.0; rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(f1s) / len(f1s) if f1s else 0.0

def probs(ans):
    if "probabilities" in ans and ans["probabilities"]:
        return {str(k): float(v) for k, v in ans["probabilities"].items()}
    if "noul" in ans:
        return {"false": 1 - float(ans["noul"]), "true": float(ans["noul"])}
    return {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True); ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    agent = laya.load(a.ckpt)
    rows = [json.loads(l) for l in open(a.data)]
    if a.limit: rows = rows[: a.limit]

    lat, recs = [], []
    agent.predict("warm up", QUESTIONS)
    for r in rows:
        t = time.perf_counter(); ans = agent.predict(r["state"], QUESTIONS)["answers"]; lat.append((time.perf_counter() - t) * 1000)
        recs.append({"exp": r["expected"], "p": {q: probs(ans[q]) for q in QUESTIONS}})

    rep = {"ckpt": a.ckpt, "data": a.data, "n": len(recs)}
    for q in ("sentiment", "intent"):
        y = [x["exp"][q] for x in recs]; p = [max(x["p"][q], key=x["p"][q].get) for x in recs]
        conf = [max(x["p"][q].values()) for x in recs]; cor = [int(i == j) for i, j in zip(y, p)]
        rep[q] = {"accuracy": round(sum(cor) / len(cor), 4), "ece": round(ece(conf, cor), 4),
                  "macro_f1": round(macro_f1(y, p, SENTIMENTS if q == "sentiment" else INTENTS), 4)}
    y_u = [x["exp"]["urgency"] for x in recs]; p_u = [int(max(x["p"]["urgency"], key=x["p"]["urgency"].get)) for x in recs]
    rep["urgency"] = {"accuracy": round(sum(i == j for i, j in zip(y_u, p_u)) / len(recs), 4),
                      "within_one": round(sum(abs(i - j) <= 1 for i, j in zip(y_u, p_u)) / len(recs), 4)}

    # escalation: p_esc = max(P(escalate), P(angry)) -- the TDD's asymmetric-threshold signal
    pos = [x["exp"]["escalate"] for x in recs]
    p_esc = [max(x["p"]["escalate"].get("true", 0.0), x["p"]["sentiment"].get("angry", 0.0)) for x in recs]
    sweep = []
    for t in [i / 100 for i in range(5, 96, 5)]:
        tp = sum(p >= t and y for p, y in zip(p_esc, pos)); fn = sum(p < t and y for p, y in zip(p_esc, pos))
        fp = sum(p >= t and not y for p, y in zip(p_esc, pos)); tn = sum(p < t and not y for p, y in zip(p_esc, pos))
        sweep.append({"tau": t, "recall": round(tp / (tp + fn), 4) if tp + fn else None,
                      "false_escalation_rate": round(fp / (fp + tn), 4) if fp + tn else None,
                      "escalated_share": round((tp + fp) / len(recs), 4)})
    full = [s for s in sweep if s["recall"] == 1.0]
    rep["escalation"] = {"positives": sum(pos), "tau_esc_max_recall1": full[-1] if full else None, "sweep": sweep}
    rep["latency_ms"] = {"p50": round(statistics.median(lat), 1), "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1),
                         "device": str(getattr(agent, "device", "?"))}
    json.dump(rep, open(a.out, "w"), indent=1)
    short = {k: v for k, v in rep.items() if k != "escalation"}
    short["escalation"] = {"positives": rep["escalation"]["positives"], "tau_esc_max_recall1": rep["escalation"]["tau_esc_max_recall1"]}
    print(json.dumps(short, indent=1))

if __name__ == "__main__":
    main()
