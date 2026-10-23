# Conductor plan: tests + RAG corpus v2 (2026-10-23)

## Original request (user)
"Elephant and goldfish": Opus orchestrates, and Flash workers implement. Everything must be validated,
with pytest coverage for all of it. The task is to do the index-side RAG improvements (regroup the
`other` theme, merge overlapping articles, add uncovered topics), re-evaluate before and after, and
then push to GitHub.

## Repo
- Path: /usr/local/google/home/susmith/src/support-ticket-router-capstone. This is a plain local git repo, not CitC.
- Read AGENTS.md, rag/README.md and rag/EVAL_RESULTS.md for context.
- Test runner: `.venv/bin/python -m pytest` (pytest 9.1.1 is already installed in `.venv`). Install
  extra packages with `~/.local/bin/uv pip install --python .venv/bin/python <pkg>`.
- Scratch data, which must never be committed:
  /usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/
  (faq/docs/*.txt = v1 articles, threads.jsonl, labels.jsonl, clean_eval/).
- GCP project: susmith-fde-ticket-router. For tokens, use
  `gcloud auth application-default print-access-token`. Do not use the gcloud CLI token
  (it is CBA-bound).
- The pre-tool hook blocks `python3 -c`, `grep` and `find`. Write script files instead and use the file tools.

## Hard rules for all workers
- Unit tests are OFFLINE. They make no network, GCP or LLM calls. Mock `urllib.request.urlopen`,
  token fetches and subprocess calls.
- Do NOT `git commit`, `git add`, `git checkout`, `git restore` or `git stash`. The conductor commits.
- Edit only the files in your ASSIGNED_FILES_EXCLUSIVE list.
- Refactors of existing scripts must preserve CLI behavior: same args, same output files, same JSON keys.
  Allowed refactors: move module-level code under `main()` / `if __name__ == "__main__":`, and extract
  pure helper functions.
- Do not weaken a test or mark it skip/xfail to get a green run.
- Return header (first 3 lines):
  STATUS / MODIFIED_FILES / VERIFY_RECEIPT. The receipt must include the exact pytest command, its exit
  code, and the summary line, for example `NN passed in X.XXs`.

## Workstream A: test suite + test infra (worker A)
Goal: pytest coverage for every pure-logic function in rag/ and training/router_s1/pipeline/.

Coverage required:
- rag:
  - query cleaning (`retrieval_eval.clean`)
  - recall@k / MRR computation
  - `clean_eval.norm` and the verbatim-citation check
  - `publish_faq`: `sha`, `answer_only`, `lint` (it must flag the invented "48 hours" style fact and pass
    policy-consistent text), `stamp` (version bump only when content changes, supersedes chain,
    effective dates), `load_manifest`
  - `evalsvc_groundedness` body construction, retry on 429/timeout, and score aggregation (mock urlopen)
  - `clean_eval_breakdown` escalation logic
- training: `prepare_twcs.redact` / `luhn_ok` (keep the existing test_redact cases), `evaluate.ece`, `macro_f1`,
  `sweep`, `golden_score.kappa` / `norm`, `label_teacher.to_gold`, `simulate_system` (tier0 regexes,
  non_english, shouting, `run` routing), `strict_recall`, `build_dataset.argmax`, `router_schema`
  invariants.

Infra:
- tests/ tree (tests/rag, tests/training), conftest.py with sys.path setup, pytest.ini (testpaths, -q).
- requirements-dev.txt.
- A Makefile or a `scripts/test.sh` one-liner.
- Move training/router_s1/pipeline/test_redact.py into tests/training/ with the same cases.

New feature (needed by workstream B's eval):
- `--gold-map <json>` option for `rag/retrieval_eval.py` and `rag/clean_eval.py`. The map goes from the old
  doc/theme id to a new doc id. When it is set, a retrieved doc counts as a hit if it equals
  gold_map.get(gold, gold).
- `clean_eval.py` must be able to REUSE an existing questions.jsonl (skip question generation) through
  `--reuse-questions`.
- Write tests for both.

ASSIGNED_FILES_EXCLUSIVE (A):
- tests/**, conftest.py, pytest.ini, requirements-dev.txt, Makefile, scripts/test.sh
- rag/retrieval_eval.py, rag/clean_eval.py, rag/clean_eval_breakdown.py, rag/evalsvc_groundedness.py,
  rag/publish_faq.py, rag/search_test.py, rag/show_retrieval.py, rag/build_threads_corpus.py
- training/router_s1/pipeline/*.py (refactor-for-import only, plus deleting the moved test_redact.py)
- README.md: a "Running tests" section only

## Workstream B: corpus v2 builder (worker B). Phase 1 is LOCAL ONLY: no Agent Search import.
Goal: produce faq v2 locally plus a gold map from v1 to v2.

Steps:
1. `rag/regroup_themes.py`: themes_v1.json + labels.jsonl + threads.jsonl -> themes_v2.json + gold_map_v1_v2.json.
   a. MERGE overlapping order-status themes into one article each. Use your judgment from titles and
      descriptions, and report the exact merge map. At minimum:
      - late general delivery / late prime (Hub Plus) delivery / general order not arrived / order not
        shipped -> one "late or not-arrived order" article
      - missing-delivered / stolen -> evaluate whether to merge
   b. SPLIT the `other` intent family. Re-cluster its threads (Gemini 3.1 Pro preview, structured JSON,
      reuse `build_faq_corpus.gemini`) into coherent sub-themes. Name them; drop pure
      praise/noise. Uncovered topics seen in misses: devices (Kindle/Echo/Fire), Prime Video,
      app/website issues.
   c. The deterministic logic (merge application, id mapping, thread reassignment, validation that every
      v1 theme maps to some v2 doc or is explicitly dropped) must be pure functions with tests. The LLM
      call must be injectable so tests use a fake.
2. `rag/build_faq_corpus.py`:
   - add `--themes <path>` and `--out <dir>`
   - add a `--reuse-from <v1 docs dir>` mode that copies unchanged themes' articles verbatim, so only
     new or merged themes are synthesized
   - keep the v1 CLI default unchanged
   - fix the invented "respond within 48 hours" fact: the prompt must forbid numbers not in
     SIMULATED_POLICY, and run `publish_faq.lint` from Python on each new article, regenerating once
     on a lint failure
3. Run it for real. The output goes to the scratch dir `scratch/rag/faq_v2/` (docs/, import jsonl,
   themes_v2.json, gold_map_v1_v2.json). Report:
   - article count
   - merge map
   - new themes
   - measured token usage
   - lint results

ASSIGNED_FILES_EXCLUSIVE (B):
- rag/regroup_themes.py, rag/build_faq_corpus.py
- tests/rag/test_regroup_themes.py, tests/rag/test_build_faq_corpus.py

Read-only for B: rag/publish_faq.py. Import `lint` from it, but do not edit it.

## Billing decision (user, 2026-10-23 20:01)
Offline LLM work runs on Jetski agents (latest Gemini Flash, gemini-3.8-flash-high), NOT on Vertex
in Argolis. That covers article synthesis, regrouping, and future eval question generation and
judging. Only Agent Search queries and the system's runtime answer calls stay on Argolis. Agent
decisions are stored as data files, so builds stay reproducible from them.

## Phase 2 (after conductor review)
Publish v2 via publish_faq.py, then:
- `retrieval_eval.py --threshold LOWEST --clean --gold-map`
- `clean_eval.py --reuse-questions --gold-map`

Gate: Δrecall@5 ≥ −0.02 overall and ≥ −0.05 per intent, otherwise roll back to v1. After that,
update EVAL_RESULTS.md and push.
