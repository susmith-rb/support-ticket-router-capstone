import collections
import json
import sys

L = sys.argv[1]
cat = {d["doc_id"] for d in json.load(open(f"{L}/catalog.json"))}
allrows = []
for s in range(4):
    sh = {json.loads(l)["qid"]: json.loads(l) for l in open(f"{L}/shard_{s}.jsonl")}
    rows = [json.loads(l) for l in open(f"{L}/labels_C2_shard_{s}.jsonl")]
    qids = [r["qid"] for r in rows]
    assert len(set(qids)) == 150 and set(qids) == set(sh), s
    miss_verdict = sum(1 for r in rows if r["support_request"] and r["primary"] and set(sh[r["qid"]]["candidates"]) - set(r.get("verdicts", {})))
    notes = collections.Counter(r["note"] for r in rows)
    vtxt = collections.Counter(v for r in rows for v in r.get("verdicts", {}).values())
    ans = [r for r in rows if r["primary"]]
    bad = sum(1 for r in ans if r["primary"] not in r["relevant"] or len(r["relevant"]) > 3 or set(r["relevant"]) - cat)
    print(f"shard {s}: answerable {len(ans)} mean|rel| {sum(len(r['relevant']) for r in ans)/len(ans):.2f} dist {dict(collections.Counter(len(r['relevant']) for r in ans))} missing-verdicts {miss_verdict} invalid {bad} unique-notes {len(notes)} top-verdict-texts {vtxt.most_common(2)}")
    allrows += rows
with open(f"{L}/labels_C2.jsonl", "w") as f:
    for r in allrows:
        f.write(json.dumps({k: r[k] for k in ("qid", "support_request", "language", "primary", "relevant", "note")} | {"source": "pro-shard"}, ensure_ascii=False) + "\n")
C = {(x := json.loads(l))["qid"]: x for l in open(f"{L}/labels_C.jsonl")}
C2 = {r["qid"]: r for r in allrows}
print("C vs C2 primary agree", sum(C[q]["primary"] == C2[q]["primary"] for q in C), "/600; mean jaccard", round(sum((len(set(C[q]["relevant"]) & set(C2[q]["relevant"])) / len(set(C[q]["relevant"]) | set(C2[q]["relevant"])) if set(C[q]["relevant"]) | set(C2[q]["relevant"]) else 1) for q in C) / 600, 3))
