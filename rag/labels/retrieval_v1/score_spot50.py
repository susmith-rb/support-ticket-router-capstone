import json,sys,math
S=sys.argv[1]; L=f"{S}/labeling"
E={(x:=json.loads(l))["qid"]:x for l in open(f"{L}/labels_EXPERT_spot50.jsonl")}
def ld(n): return {(x:=json.loads(l))["qid"]:x for l in open(f"{L}/{n}")}
C2=ld("labels_C2.jsonl"); C=ld("labels_C.jsonl")
run=json.load(open(f"{S}/ablation/test_rows_r5.json")); res=run.get("results") or next(v for v in run.values() if isinstance(v,dict) and "results" in v)["results"]
rows={r["qid"]:r for r in [c for c in res if c["config"]=="intent_soft_noisy_0.2"][0]["rows"]}
for nm,X in (("C2(Pro)",C2),("C(Flash)",C)):
    sup=sum(E[q]["support_request"]==X[q]["support_request"] for q in E)
    both=[q for q in E if E[q]["primary"] and X[q]["primary"]]
    prim=sum(E[q]["primary"]==X[q]["primary"] for q in both)
    inrel=sum(E[q]["primary"] in X[q]["relevant"] for q in both)
    jac=sum(len(set(E[q]["relevant"])&set(X[q]["relevant"]))/len(set(E[q]["relevant"])|set(X[q]["relevant"])) for q in both)/len(both)
    print(f"Expert vs {nm}: support agree {sup}/50 | primary exact {prim}/{len(both)} | expert-primary in {nm} relevant {inrel}/{len(both)} | jaccard {jac:.2f}")
def score(lab):
    qs=[q for q in E if lab[q]["support_request"] and lab[q]["primary"] and lab[q]["language"]=="en"]
    r5=mrr=nd=0
    for q in qs:
        top=[t if isinstance(t,str) else t.get("id") for t in (rows[q].get("top10_ids") or rows[q]["top10"])]
        rel=set(lab[q]["relevant"]); p=lab[q]["primary"]; rk=[i+1 for i,d in enumerate(top) if d in rel]
        r5+=bool(rk) and rk[0]<=5; mrr+=1/rk[0] if rk else 0
        dcg=sum((2 if d==p else 1 if d in rel else 0)/math.log2(i+2) for i,d in enumerate(top[:5]))
        ideal=[2]+[1]*(len(rel)-1); nd+=dcg/sum(v/math.log2(i+2) for i,v in enumerate(ideal))
    n=len(qs); return f"n={n} r@5 {r5/n:.3f} MRR {mrr/n:.3f} nDCG {nd/n:.3f}"
print("System on 50 with EXPERT labels:",score(E)); print("System on 50 with C2 labels:    ",score(C2)); print("System on 50 with C labels:     ",score(C))
