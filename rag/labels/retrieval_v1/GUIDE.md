# Blind relevance labeling guide (TDD §6.4 `retrieval/v1`)

You are an annotator. For each customer message, decide which FAQ articles (from catalog.json) would
correctly help answer it. You are BLIND by design. Do NOT open or search for any other files in the
scratch directory, including retrieval results, eval files, gold maps, threads, ablation outputs, or any
other annotator's labels. Do NOT call any search API, embedding, or LLM API, and do NOT write a script that
assigns labels by keyword matching, similarity, or rules. Every label must be YOUR OWN judgment, made by
reading the message and the catalog. Writing a small script to READ inputs and WRITE your judgments to the
output file is fine.

Inputs (read-only):
- catalog.json: 50 articles with doc_id, title, sample_questions and summary.
- queries_blind.jsonl: 600 messages as {qid, query}. They are real customer tweets: noisy, sarcastic, and
  sometimes not English.

For each qid, output one JSON line:
```
{"qid": "...", "support_request": true|false, "language": "en"|"other",
 "primary": "<doc_id or null>", "secondary": ["<doc_id>", ...], "note": "<= 12 words why"}
```

Rules:
- `support_request`: true if the message asks for help, or reports a problem a support FAQ could
  address. False for pure praise, jokes, chatter, or content-free text (e.g. "😐 No words [URL]").
- `language`: "other" if the message is mainly not English.
- `primary`: the ONE best article that answers the customer's need. Use null if no article in the catalog
  reasonably helps, or if support_request is false.
- `secondary`: 0–2 OTHER articles that would ALSO genuinely help (a reasonable agent could cite them).
  Don't pad: include one only if you'd be satisfied seeing it as the answer. It must be different from
  primary.
- Judge by the customer's actual need, not by keyword overlap.
- Ambiguous messages: choose the most plausible need. If two needs are equally plausible, make one primary
  and the other secondary.
- Language matters only for the language flag; still label the need if you can understand it.

Work in batches (e.g. 50 at a time) and append to your output file. Before you finish, validate:
- exactly 600 unique qids
- every doc_id exists in the catalog
- no doc_id repeated within a line
Report counts: support_request=false, language=other, primary=null, and mean number of relevant docs.
