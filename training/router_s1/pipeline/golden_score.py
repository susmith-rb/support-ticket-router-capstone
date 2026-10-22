#!/usr/bin/env python3
"""golden_score.py — score teacher (Gemini) and Laya against the human golden set.

Inputs: two CSV exports of the labeling sheet tabs (Labeler A, Labeler B) and golden_key.jsonl.
  gsheets readonly export <SHEET_ID> a.csv --format csv --sheet "Labeler A"   (same for B)
Reports:
  * inter-annotator agreement per question (raw % and Cohen's kappa) -> is the task well defined?
  * human consensus = rows where A and B agree (disagreements listed for adjudication)
  * teacher accuracy and Laya accuracy vs consensus, per stratum and re-weighted to the test mix
  * angry/urgent recall vs humans (consensus angry OR urgency=3) for teacher and Laya @ tau_esc

  python golden_score.py a.csv b.csv golden_key.jsonl [--tau-esc 0.5] [--tau-low 0.15]
"""
import argparse, collections, csv, json

Q = ("escalate", "sentiment", "urgency", "intent")

def norm(q, v):
    v = (v or "").strip().lower()
    if not v: return None
    if q == "escalate": return "true" if v in ("yes", "true") else "false"
    if q == "urgency": return v.split(" ")[0]
    return v

def load(path):
    out = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        out[r["case_id"]] = {q: norm(q, r.get(q)) for q in Q} | {"unsure": (r.get("unsure") or "").strip().lower() == "yes"}
    return out

def kappa(pairs):
    n = len(pairs)
    if not n: return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = collections.Counter(a for a, _ in pairs), collections.Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0

ap = argparse.ArgumentParser()
ap.add_argument("a"); ap.add_argument("b"); ap.add_argument("key")
ap.add_argument("--tau-esc", type=float, default=0.5); ap.add_argument("--tau-low", type=float, default=0.15)
x = ap.parse_args()
A, B = load(x.a), load(x.b)
key = {json.loads(l)["case_id"]: json.loads(l) for l in open(x.key)}

print("INTER-ANNOTATOR AGREEMENT")
for q in Q:
    pairs = [(A[c][q], B[c][q]) for c in key if A.get(c, {}).get(q) and B.get(c, {}).get(q)]
    print(f"  {q:10s} n={len(pairs):3d}  agree {sum(a == b for a, b in pairs) / max(1, len(pairs)):.3f}  kappa {kappa(pairs)}")

print("\nACCURACY vs HUMAN CONSENSUS (A == B)   [weighted = re-weighted to the test distribution]")
for q in Q:
    res = collections.defaultdict(lambda: [0, 0, 0.0, 0.0])  # who -> hits, n, whits, wn
    disagreements = 0
    for c, k in key.items():
        a, b = A.get(c, {}).get(q), B.get(c, {}).get(q)
        if not a or not b: continue
        if a != b: disagreements += 1; continue
        t = str(k["teacher"][q]).lower(); l = str(k["laya"][q]).lower()
        for who, pv in (("teacher", t), ("laya", l)):
            for s in (who, f"{who}:{k['stratum']}"):
                r = res[s]; r[0] += pv == a; r[1] += 1; r[2] += k["weight"] * (pv == a); r[3] += k["weight"]
    line = f"  {q:10s} (A!=B: {disagreements:3d}) "
    for who in ("teacher", "laya"):
        r = res[who]
        line += f"| {who} {r[0] / max(1, r[1]):.3f} (w {r[2] / max(1e-9, r[3]):.3f}) "
    print(line)
    for s in ("split", "strict", "rest"):
        t, l = res[f"teacher:{s}"], res[f"laya:{s}"]
        print(f"      {s:6s} n={t[1]:3d}  teacher {t[0] / max(1, t[1]):.3f}  laya {l[0] / max(1, l[1]):.3f}")

print("\nANGRY/URGENT RECALL vs HUMANS (consensus sentiment=angry OR urgency=3)")
pos = [c for c, k in key.items() if A.get(c, {}).get("sentiment") and A[c]["sentiment"] == B.get(c, {}).get("sentiment")
       and A[c]["urgency"] == B.get(c, {}).get("urgency") and (A[c]["sentiment"] == "angry" or A[c]["urgency"] == "3")]
if pos:
    t_hit = sum(key[c]["teacher"]["sentiment"] == "angry" or str(key[c]["teacher"]["urgency"]) == "3" for c in pos)
    l_esc = sum(key[c]["laya"]["p_esc"] >= x.tau_esc for c in pos)
    l_t1 = sum(key[c]["laya"]["p_esc"] >= x.tau_low for c in pos)
    print(f"  positives={len(pos)}  teacher recall {t_hit / len(pos):.3f}  laya@tau_esc={x.tau_esc} {l_esc / len(pos):.3f}  "
          f"laya not-auto (p_esc >= tau_low={x.tau_low}) {l_t1 / len(pos):.3f}")
    for c in pos:
        if key[c]["laya"]["p_esc"] < x.tau_low: print("   laya auto-routed:", c, key[c]["laya"]["p_esc"])
