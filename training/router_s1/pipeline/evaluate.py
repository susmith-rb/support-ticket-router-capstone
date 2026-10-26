#!/usr/bin/env python3
"""evaluate.py — score a Laya checkpoint on a laya-train JSONL split, router-style.

Runs in the SERVING venv (laya 0.3.20, what the Model Garden container pins), so it also proves
the checkpoint is servable. Reports:
  * per-question accuracy vs teacher argmax, macro-F1, ECE
  * escalation sweeps for three p_esc signals (TDD §5.2): escalate / max(escalate, angry) /
    max(escalate, angry, P(urgency=immediate)), plus recall on unanimous (3/3) positives
  * a tiered-routing simulation: Tier-1 decides above tau_esc (escalate) and below tau_low (auto);
    the band in between goes to Tier 2 (Gemini). Reports Tier-2 share, auto share, Tier-1
    precision, and the system recall bound if Tier 2 matches the teacher
  * the missed positives at the chosen operating point, for error analysis
  * in-process latency

  python evaluate.py --ckpt runs/v1/ckpt --data data/dataset/v1/test.jsonl --out runs/v1/eval_test.json
  python evaluate.py --ckpt convaiinnovations/laya --data ... --out ...              # base `english`
  python evaluate.py --ckpt convaiinnovations/laya --subfolder typed-decisions ...    # base typed-decisions
"""
import argparse
import json
import statistics
import time
from training.router_s1.pipeline.router_schema import INTENTS, QUESTIONS, SENTIMENTS

TAUS = [round(i / 100, 2) for i in range(1, 100)]

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
    if ans.get("probabilities"):
        return {str(k): float(v) for k, v in ans["probabilities"].items()}
    if "noul" in ans:
        return {"false": 1 - float(ans["noul"]), "true": float(ans["noul"])}
    return {}

def sweep(scores, pos):
    out = []
    P = sum(pos); N = len(pos) - P
    for t in TAUS:
        tp = sum(s >= t and y for s, y in zip(scores, pos)); fp = sum(s >= t and not y for s, y in zip(scores, pos))
        out.append({"tau": t, "recall": round(tp / P, 4) if P else None,
                    "false_escalation_rate": round(fp / N, 4) if N else None,
                    "escalated_share": round((tp + fp) / len(pos), 4)})
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True); ap.add_argument("--subfolder", default=None)
    ap.add_argument("--data", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dump", default=None, help="write per-row predictions (JSONL) for system simulation")
    a = ap.parse_args()
    import laya
    agent = laya.load(a.ckpt, subfolder=a.subfolder) if a.subfolder else laya.load(a.ckpt)
    rows = [json.loads(l) for l in open(a.data)]
    if a.limit: rows = rows[: a.limit]

    lat, recs = [], []
    agent.predict("warm up", QUESTIONS)
    for r in rows:
        t = time.perf_counter(); ans = agent.predict(r["state"], QUESTIONS)["answers"]; lat.append((time.perf_counter() - t) * 1000)
        recs.append({"text": r["state"], "exp": r["expected"], "gold_p_esc": r["gold"]["escalate"]["probabilities"]["true"],
                     "p": {q: probs(ans[q]) for q in QUESTIONS}})

    rep = {"ckpt": a.ckpt + (f"/{a.subfolder}" if a.subfolder else ""), "data": a.data, "n": len(recs)}
    if a.dump:
        with open(a.dump, "w") as fh:
            for x in recs:
                fh.write(json.dumps({"text": x["text"], "expected": x["exp"], "gold_p_esc": x["gold_p_esc"], "p": x["p"]}) + "\n")
    for q, labels in (("sentiment", SENTIMENTS), ("intent", INTENTS)):
        y = [x["exp"][q] for x in recs]; p = [max(x["p"][q], key=x["p"][q].get) for x in recs]
        conf = [max(x["p"][q].values()) for x in recs]; cor = [int(i == j) for i, j in zip(y, p)]
        rep[q] = {"accuracy": round(sum(cor) / len(cor), 4), "ece": round(ece(conf, cor), 4),
                  "macro_f1": round(macro_f1(y, p, labels), 4)}
    y_u = [x["exp"]["urgency"] for x in recs]; p_u = [int(max(x["p"]["urgency"], key=x["p"]["urgency"].get)) for x in recs]
    rep["urgency"] = {"accuracy": round(sum(i == j for i, j in zip(y_u, p_u)) / len(recs), 4),
                      "within_one": round(sum(abs(i - j) <= 1 for i, j in zip(y_u, p_u)) / len(recs), 4)}

    pos = [x["exp"]["escalate"] for x in recs]
    strong = [x["gold_p_esc"] >= 0.999 for x in recs]
    top = str(len(QUESTIONS["urgency"]["criteria"]) - 1)
    e_yes = [x["p"]["escalate"].get("true", 0.0) for x in recs]
    e_ang = [x["p"]["sentiment"].get("angry", 0.0) for x in recs]
    e_urg = [x["p"]["urgency"].get(top, 0.0) for x in recs]
    signals = {"escalate": e_yes,
               "max_escalate_angry": [max(a, b) for a, b in zip(e_yes, e_ang)],
               "max_escalate_angry_urgent": [max(a, b, c) for a, b, c in zip(e_yes, e_ang, e_urg)]}
    esc = {"positives": sum(pos), "unanimous_positives": sum(strong), "signals": {}}
    for name, s in signals.items():
        sw = sweep(s, pos); sw_strong = sweep(s, strong)
        full = [x for x in sw if x["recall"] == 1.0]; full_s = [x for x in sw_strong if x["recall"] == 1.0]
        esc["signals"][name] = {"tau_max_recall1_all": full[-1] if full else None,
                                "tau_max_recall1_unanimous": full_s[-1] if full_s else None,
                                "min_score_of_any_positive": round(min(v for v, y in zip(s, pos) if y), 4),
                                "sweep": sw}
    rep["escalation"] = esc

    s = signals["max_escalate_angry_urgent"]; P = sum(pos)
    sim = []
    for t_low in (0.02, 0.05, 0.1, 0.15, 0.2):
        for t_esc in (0.3, 0.4, 0.5, 0.6, 0.7):
            band = [t_low <= v < t_esc for v in s]; hi = [v >= t_esc for v in s]; lo = [v < t_low for v in s]
            t1_tp = sum(h and y for h, y in zip(hi, pos)); t1_fp = sum(h and not y for h, y in zip(hi, pos))
            missed_lo = sum(l and y for l, y in zip(lo, pos))
            sim.append({"tau_low": t_low, "tau_esc": t_esc,
                        "tier2_share": round(sum(band) / len(s), 4), "auto_share": round(sum(lo) / len(s), 4),
                        "tier1_escalate_share": round(sum(hi) / len(s), 4),
                        "tier1_precision": round(t1_tp / max(1, t1_tp + t1_fp), 4),
                        "system_recall_if_tier2_matches_teacher": round(1 - missed_lo / P, 4) if P else None,
                        "missed_below_tau_low": missed_lo})
    rep["tiered_simulation"] = sim
    best = [x for x in sim if x["missed_below_tau_low"] == 0]
    rep["tiered_best_recall1"] = min(best, key=lambda x: x["tier2_share"]) if best else None
    rep["missed_positives_low_score"] = [
        {"text": x["text"], "gold_p_escalate": round(x["gold_p_esc"], 3), "p_esc": round(v, 4),
         "pred": {q: max(x["p"][q], key=x["p"][q].get) for q in ("sentiment", "intent")}}
        for x, v, y in sorted(zip(recs, s, pos), key=lambda z: z[1]) if y][:12]
    rep["latency_ms"] = {"p50": round(statistics.median(lat), 1), "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1),
                         "device": str(getattr(agent, "device", "?"))}
    json.dump(rep, open(a.out, "w"), indent=1)

    short = {k: rep[k] for k in ("ckpt", "n", "sentiment", "intent", "urgency", "latency_ms", "tiered_best_recall1")}
    short["escalation"] = {n: {k: v for k, v in d.items() if k != "sweep"} for n, d in esc["signals"].items()}
    short["escalation"]["positives"] = esc["positives"]; short["escalation"]["unanimous_positives"] = esc["unanimous_positives"]
    print(json.dumps(short, indent=1))

if __name__ == "__main__":
    main()
