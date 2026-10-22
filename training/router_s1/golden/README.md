# Golden set labeling (offline copy)

`golden_set_labeling.xlsx` is an export of the labeling Google Sheet, so it can be opened
anywhere: Excel, LibreOffice, or any Google account (Google Sheets → File → Import → Upload).

**Data:** 300 PII-redacted customer tweets to @AmazonHelp from the public Kaggle dataset
*Customer Support on Twitter* (thoughtvector).

## Tabs

| Tab | Purpose |
| --- | --- |
| Instructions | Label definitions and workflow |
| Review A (146) / Review B (146) | **What to label:** 116 cases where Gemini 3.1 Pro, Claude Sonnet 5.5 and Claude Opus 5.5 disagreed, plus 30 spot-checks where all three agreed. One tab per labeler, done independently |
| Labeler A / Labeler B (300) | Optional full set |
| *All model outputs* (hidden) | Gemini teacher, Laya v2, Claude Sonnet 5.5, Claude Opus 5.5 side by side. Open it only after you've finished labeling |
| *Model: Claude Sonnet 5.5 / Opus 5.5* (hidden) | Per-model labels |

## Round trip

1. Download the `.xlsx` and fill in your Review tab (the dropdowns still work).
2. Send it back: commit the filled file as `golden_set_labeling_<name>.xlsx` in this folder, or
   export your Review tab as CSV.
3. Scoring: `python pipeline/golden_score.py reviewA.csv reviewB.csv golden_key.jsonl`
   (`golden_key.jsonl`, the answer key, lives in `gs://susmith-fde-ticket-router-laya/data/golden/v1/`).
