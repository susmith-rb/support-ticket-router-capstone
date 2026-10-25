import json
import math
import sys

S = sys.argv[1]
C2 = {(x := json.loads(l))["qid"]: x for l in open(f"{S}/labeling/labels_C2.jsonl")}
d = json.load(open(f"{S}/ablation/dev_rows_phaseD.json"))
res = d.get("results") or next(v for v in d.values() if isinstance(v, dict) and "results" in v)["results"]
rows = {
    r["qid"]: (r.get("top10_ids") or r.get("top10") or [])
    for r in [c for c in res if c["config"] == "intent_noisy_0.2"][0]["rows"]
}
nd = []
full = 0
for q, l in C2.items():
    if not (q in rows and l["support_request"] and l["primary"] and l["language"] == "en"):
        continue
    rel = set(l["relevant"])
    p = l["primary"]

    def g(x, p=p, rel=rel):
        return 2 if x == p else 1 if x in rel else 0

    best = sorted(rows[q], key=lambda x: -g(x))[:5]
    dcg = sum(g(x) / math.log2(i + 2) for i, x in enumerate(best))
    ideal = sum(v / math.log2(i + 2) for i, v in enumerate([2] + [1] * (len(rel) - 1)))
    nd.append(dcg / ideal)
    full += set(rel) <= set(rows[q])
print(
    "n",
    len(nd),
    "ORACLE nDCG@5 from top-10:",
    round(sum(nd) / len(nd), 3),
    "| all relevant docs inside top-10:",
    full,
    "/",
    len(nd),
    "| primary in top-10:",
    sum(
        1
        for q, l in C2.items()
        if q in rows and l["support_request"] and l["primary"] and l["language"] == "en" and l["primary"] in rows[q]
    ),
)
