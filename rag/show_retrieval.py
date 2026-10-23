import json, sys
d = json.load(open(sys.argv[1])); r = d["report"]
print({k: v for k, v in r.items() if k not in ("by_intent", "misses_sample")})
rows = d["rows"]
empty = sum(1 for x in rows if not x["top3"])
print("queries with ZERO results:", empty, "/", len(rows))
nonempty = [x for x in rows if x["top3"]]
if nonempty:
    hit5 = sum(1 for x in nonempty if x["rank"] and x["rank"] <= 5)
    print("recall@5 among queries that returned results:", round(hit5 / len(nonempty), 3), "n=", len(nonempty))
