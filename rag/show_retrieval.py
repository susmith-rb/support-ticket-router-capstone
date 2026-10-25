#!/usr/bin/env python3
import json
import sys


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: python show_retrieval.py <retrieval_eval.json>")
        return 0
    d = json.load(open(args[0]))
    r = d["report"]
    print({k: v for k, v in r.items() if k not in ("by_intent", "misses_sample")})
    rows = d["rows"]
    empty = sum(1 for x in rows if not x["top3"])
    print("queries with ZERO results:", empty, "/", len(rows))
    nonempty = [x for x in rows if x["top3"]]
    if nonempty:
        hit5 = sum(1 for x in nonempty if x["rank"] and x["rank"] <= 5)
        print("recall@5 among queries that returned results:", round(hit5 / len(nonempty), 3), "n=", len(nonempty))
    return 0


if __name__ == "__main__":
    sys.exit(main())
