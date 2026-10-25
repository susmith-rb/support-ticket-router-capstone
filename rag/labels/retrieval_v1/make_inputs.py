import json
import pathlib
import random
import re
import sys

S = pathlib.Path(sys.argv[1])
out = S / "labeling"
cat = []
for p in sorted((S / "faq_v2p/docs").glob("*.txt")):
    t = p.read_text()
    lines = t.splitlines()
    title = lines[0].lstrip("# ").strip()
    para = [l[2:].strip() for l in lines if l.startswith("- ")][:8]
    body = re.sub(r"\s+", " ", " ".join(l for l in lines[1:] if l and not l.startswith("- ") and not l.startswith("Also asked")))[:350]
    fam = p.stem.split("-")[1] + "_" + p.stem.split("-")[2] if p.stem.split("-")[1] in ("order", "account", "returns", "shipping") else "other"
    cat.append({"doc_id": p.stem, "title": title, "sample_questions": para, "summary": body})
json.dump(cat, open(out / "catalog.json", "w"), indent=1)
qs = []
for split in ("test", "dev"):
    for l in open(S / f"eval/{split}.jsonl"):
        r = json.loads(l)
        qs.append({"qid": r["qid"], "query": r["query"]})
random.Random(7).shuffle(qs)
with open(out / "queries_blind.jsonl", "w") as f:
    for q in qs:
        f.write(json.dumps(q, ensure_ascii=False) + "\n")
print(len(cat), "articles;", len(qs), "queries (test+dev shuffled, no gold, no system output)")
