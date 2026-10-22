import json, sys
key = {json.loads(l)["case_id"]: json.loads(l) for l in open(sys.argv[1])}
mods = {"sonnet": {json.loads(l)["case_id"]: json.loads(l) for l in open(sys.argv[2])},
        "opus": {json.loads(l)["case_id"]: json.loads(l) for l in open(sys.argv[3])}}
W = {"rest": 429 / 646, "strict": 111 / 646, "split": 106 / 646}
cnt = {s: sum(1 for k in key.values() if k["stratum"] == s) for s in W}
def lv(c, q):
    v = key[c]["laya"][q]; return str(v).lower()
for q in ("escalate", "sentiment", "urgency", "intent"):
    out = []
    for ref in ("gemini", "sonnet", "opus"):
        raw = wsum = 0.0
        for c, k in key.items():
            r = str(k["teacher"][q]).lower() if ref == "gemini" else str(mods[ref][c]["majority"][q]).lower()
            hit = lv(c, q) == r; raw += hit; wsum += hit * W[k["stratum"]] / cnt[k["stratum"]]
        out.append(f"laya vs {ref} {raw / len(key):.3f} (reweighted {wsum:.3f})")
    print(f"{q:10s} " + " | ".join(out))
dis = json.load(open(sys.argv[4]))
by = {s: sum(1 for c in dis if key[c]["stratum"] == s) for s in W}
print("3-model disagreement on escalate/sentiment, reweighted to test mix:", round(sum(W[s] * by[s] / cnt[s] for s in W), 3), by, cnt)
