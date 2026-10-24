import json,random,collections,sys
L=sys.argv[1]
q={json.loads(l)["qid"]:json.loads(l)["query"] for l in open(f"{L}/queries_blind.jsonl")}
C2={(x:=json.loads(l))["qid"]:x for l in open(f"{L}/labels_C2.jsonl")}
A={(x:=json.loads(l))["qid"]:x for l in open(f"{L}/labels_A.jsonl")}; B={(x:=json.loads(l))["qid"]:x for l in open(f"{L}/labels_B.jsonl")}
rng=random.Random(2026)
test=[k for k in q if k.startswith("t")]
fam=lambda d:(d or "none").split("-")[1] if d else "none"
dis=[k for k in test if A[k]["primary"]!=B[k]["primary"] or A[k]["support_request"]!=B[k]["support_request"]]
agr=[k for k in test if k not in dis]
pick=rng.sample(dis,15)
byf=collections.defaultdict(list)
for k in agr: byf[fam(C2[k]["primary"])].append(k)
rest=35; fams=sorted(byf); 
for i,f in enumerate(fams):
    n=round(35*len(byf[f])/len(agr)) if i<len(fams)-1 else 35-sum(1 for x in pick[15:])
    pick+=rng.sample(byf[f],min(n,len(byf[f])))
pick=pick[:50]; rng.shuffle(pick)
with open(f"{L}/spot50_blind.jsonl","w") as f:
    for k in pick: f.write(json.dumps({"qid":k,"query":q[k]},ensure_ascii=False)+"\n")
print(len(pick),"disagreement-sampled",len(set(pick)&set(dis)),collections.Counter(fam(C2[k]["primary"]) for k in pick))
