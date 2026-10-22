#!/usr/bin/env python3
"""model_tabs.py — build sheet payload for Claude label tabs + 3-way agreement vs the Gemini teacher.

  python model_tabs.py golden_key.jsonl golden_sheet_rows.json sonnet.jsonl opus.jsonl out_requests.json
"""
import json, sys
from collections import Counter

key = {json.loads(l)["case_id"]: json.loads(l) for l in open(sys.argv[1])}
msgs = dict(json.load(open(sys.argv[2])))
runs = {"Model: Claude Sonnet 5.5": sys.argv[3], "Model: Claude Opus 5.5": sys.argv[4]}
URG = {"0": "0 - can wait days", "1": "1 - within a day", "2": "2 - within hours", "3": "3 - immediate"}
Q = ("escalate", "sentiment", "urgency", "intent")

def norm(q, v):
    v = str(v).lower()
    return v

reqs, models = [], {}
for tab, path in runs.items():
    rows = {json.loads(l)["case_id"]: json.loads(l) for l in open(path)}
    models[tab] = rows
    data = [["case_id", "message", "escalate", "sentiment", "urgency", "intent", "self-agreement (min over questions, 3 votes)",
             "matches Gemini teacher (of 4)"]]
    for cid in sorted(key):
        r = rows.get(cid, {"majority": {q: "" for q in Q}, "agreement": {q: 0 for q in Q}})
        m = r["majority"]; t = key[cid]["teacher"]
        match = sum(norm(q, m[q]) == norm(q, t[q]) for q in Q if m[q] != "")
        data.append([cid, msgs[cid], "yes" if str(m["escalate"]).lower() == "true" else ("no" if m["escalate"] != "" else ""),
                     m["sentiment"], URG.get(str(m["urgency"]), ""), m["intent"],
                     round(min(r["agreement"].values()) if r["agreement"] else 0, 2), match])
    reqs.append({"op": "write", "range": f"'{tab}'!A1", "data": data})
json.dump(reqs, open(sys.argv[5], "w"))

# 3-way agreement report
names = {"gemini": None, **{t.split(": ")[1]: t for t in runs}}
print("PAIRWISE AGREEMENT on 300 golden cases (majority labels)")
def lab(src, cid, q):
    if src == "gemini": return norm(q, key[cid]["teacher"][q])
    return norm(q, models[names[src]].get(cid, {}).get("majority", {}).get(q, ""))
srcs = list(names)
for q in Q:
    parts = []
    for i in range(len(srcs)):
        for j in range(i + 1, len(srcs)):
            a, b = srcs[i], srcs[j]
            n = sum(1 for c in key if lab(a, c, q) and lab(b, c, q))
            hit = sum(1 for c in key if lab(a, c, q) and lab(a, c, q) == lab(b, c, q))
            parts.append(f"{a} vs {b} {hit / max(1, n):.3f}")
    allx = sum(1 for c in key if len({lab(s, c, q) for s in srcs}) == 1 and lab(srcs[0], c, q))
    print(f"  {q:10s} " + " | ".join(parts) + f" | all three agree {allx / len(key):.3f}")
dis = [c for c in key if any(len({lab(s, c, q) for s in srcs}) > 1 for q in ("escalate", "sentiment"))]
print(f"\ncases where the 3 models disagree on escalate or sentiment: {len(dis)} / {len(key)}  -> human review list")
by = Counter(key[c]["stratum"] for c in dis); print("  by stratum:", dict(by))
json.dump(dis, open(sys.argv[5].replace(".json", "_disagree.json"), "w"))
