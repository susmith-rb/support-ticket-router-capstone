#!/usr/bin/env python3
"""simulate_system.py — end-to-end router simulation: Tier 0 rules + Tier 1 (Laya) + Tier 2 band.

Inputs are per-row prediction dumps from evaluate.py --dump on VAL and TEST.
Operating point (tau_low) is chosen on VAL (largest tau_low with system recall 1.0), then
reported on TEST, so the rules/threshold are not fitted to the test set.

Tier 0 (deterministic, TDD §5.2):
  HARD  -> human: legal / fraud / security / safety lexicon (rules may only ADD escalations)
  SOFT  -> never auto-answer; send to Tier 2: non-English, shouting (all-caps), repeated contact,
           strong negative lexicon, explicit deadline
Tier 1: p_esc = max(P(escalate), P(angry), P(urgency=immediate))
  p_esc >= tau_esc -> human ; tau_low <= p_esc < tau_esc -> Tier 2 ; else auto (unless SOFT)
Tier 2 assumed to decide like the teacher (upper bound; Gemini Flash vs Pro agreement is
measured separately). Variant "tdd": intent=other also goes to Tier 2.

  python simulate_system.py --val runs/v2/pred_val.jsonl --test runs/v2/pred_test.jsonl --out runs/v2/system_sim.json
"""
import argparse
import json
import re

try:
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0
except Exception:  # fallback heuristic
    detect = None

HARD = re.compile(r"\b(lawyer|attorney|sue|suing|legal action|chargeback|bbb|police|fraud\w*|scam\w*|phish\w*|hack(ed|er)?|"
                  r"stolen|theft|unauthori[sz]ed|data (leak|breach)|privacy|consumer (court|forum|protection))\b", re.I)
NEG = re.compile(r"\b(worst|pathetic|pethetic|terrible|horrible|awful|disgust\w*|unacceptable|ridiculous|useless|"
                 r"never again|cancel+ing|fed up|joke)\b", re.I)
REPEAT = re.compile(r"\b(again|still|(\d+|2nd|3rd|second|third|fourth|fifth)(th)? time|several times|so many times|"
                    r"days? now|weeks? now)\b", re.I)
DEADLINE = re.compile(r"\b(today|tonight|asap|urgent\w*|immediately|right now|by \d{1,2} ?(am|pm)?|need it (by|for))\b", re.I)
CAPS_OK = {"URL", "ORDER", "EMAIL", "CARD", "PHONE", "AMAZON", "PRIME", "USA", "UK", "UPS", "USPS", "DHL", "FBA", "OTP", "EMI", "COD"}

def non_english(t):
    clean = re.sub(r"\[[A-Z]+\]|@\w+|#\w+|https?://\S+", " ", t)
    letters = [c for c in clean if c.isalpha()]
    if letters and sum(ord(c) > 127 for c in letters) / len(letters) > 0.3:
        return True
    if detect and len(clean.split()) >= 4:
        try: return detect(clean) != "en"
        except Exception: return False
    return False

def shouting(t):
    caps = [w for w in re.findall(r"[A-Za-z]{3,}", t) if w.isupper() and w not in CAPS_OK]
    return len(caps) >= 2

def tier0(t):
    soft = [n for n, f in (("non_english", non_english(t)), ("shouting", shouting(t)), ("negative", bool(NEG.search(t))),
                           ("repeat", bool(REPEAT.search(t))), ("deadline", bool(DEADLINE.search(t)))) if f]
    return bool(HARD.search(t)), soft

def p_esc(p):
    top = str(max(int(k) for k in p["urgency"]))
    return max(p["escalate"].get("true", 0.0), p["sentiment"].get("angry", 0.0), p["urgency"].get(top, 0.0))

def run(rows, tau_low, tau_esc, variant):
    P = sum(r["expected"]["escalate"] for r in rows); N = len(rows) - P
    c = {"human_hard": 0, "human_tier1": 0, "tier2": 0, "auto": 0}; caught = fp = 0; missed = []
    for r in rows:
        hard, soft = r["_t0"]; s = r["_pesc"]; y = r["expected"]["escalate"]
        intent = max(r["p"]["intent"], key=r["p"]["intent"].get)
        if hard: c["human_hard"] += 1; caught += y; fp += (not y); continue
        if s >= tau_esc: c["human_tier1"] += 1; caught += y; fp += (not y); continue
        if soft or s >= tau_low or (variant == "tdd" and intent == "other"):
            c["tier2"] += 1; caught += y; continue          # Tier 2 ~ teacher (upper bound)
        c["auto"] += 1
        if y: missed.append({"text": r["text"][:160], "gold_p_esc": round(r["gold_p_esc"], 3), "p_esc": round(s, 4)})
    n = len(rows)
    return {"tau_low": tau_low, "tau_esc": tau_esc, "variant": variant, "n": n, "positives": P,
            "system_recall_bound": round(caught / P, 4) if P else None,
            "false_escalation_rate_t0_t1": round(fp / N, 4) if N else None,
            "share": {k: round(v / n, 4) for k, v in c.items()}, "missed": missed}

def load(path):
    rows = [json.loads(l) for l in open(path)]
    for r in rows:
        r["_t0"] = tier0(r["text"]); r["_pesc"] = p_esc(r["p"])
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--val", required=True); ap.add_argument("--test", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--tau-esc", type=float, default=0.5)
    a = ap.parse_args()
    val, test = load(a.val), load(a.test)
    grid = [0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2, 0.25, 0.3]
    rep = {"rule_hit_rates_test": {}, "tier1_only_test": {}, "chosen": {}}
    for name in ("hard", "non_english", "shouting", "negative", "repeat", "deadline"):
        hits = [r for r in test if (r["_t0"][0] if name == "hard" else name in r["_t0"][1])]
        rep["rule_hit_rates_test"][name] = {"share": round(len(hits) / len(test), 4),
                                            "precision_vs_escalate": round(sum(r["expected"]["escalate"] for r in hits) / max(1, len(hits)), 3)}
    for variant in ("band_only", "tdd"):
        val_runs = [run(val, t, a.tau_esc, variant) for t in grid]
        ok = [v for v in val_runs if v["system_recall_bound"] == 1.0]
        tau_low = max(v["tau_low"] for v in ok) if ok else min(grid)
        rep["chosen"][variant] = {"tau_low_chosen_on_val": tau_low,
                                  "val": {k: v for k, v in run(val, tau_low, a.tau_esc, variant).items() if k != "missed"},
                                  "test": run(test, tau_low, a.tau_esc, variant),
                                  "test_grid": [{k: v for k, v in run(test, t, a.tau_esc, variant).items() if k != "missed"} for t in grid]}
    # Tier-1 only reference (no rules)
    for r in test: r["_t0_saved"] = r["_t0"]; r["_t0"] = (False, [])
    rep["tier1_only_test"] = {k: v for k, v in run(test, 0.15, a.tau_esc, "band_only").items() if k != "missed"}
    json.dump(rep, open(a.out, "w"), indent=1)
    print(json.dumps({"rule_hit_rates_test": rep["rule_hit_rates_test"], "tier1_only_test@0.15": rep["tier1_only_test"],
                      "chosen": {v: {"tau_low": d["tau_low_chosen_on_val"], "val": d["val"],
                                     "test": {k: x for k, x in d["test"].items() if k != "missed"},
                                     "test_missed": d["test"]["missed"][:10]} for v, d in rep["chosen"].items()}}, indent=1))

if __name__ == "__main__":
    main()
