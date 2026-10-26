"""Static analysis, contract, and policy tests for CI/CD pipelines.

Validates compliance with:
- Conductor Plan 8 (docs/plans/PLAN8.md)
- Reference architecture (adisairam-capstone-onboarding-agent)
- ai-fde-delivery skill specifications
- specs/ci-cd.md requirements (REQ-CICD-001 through REQ-CICD-008)
"""

import json
from pathlib import Path
import re
from rag.ci_eval_gate import compute_chain_version, execute_gate_check, main

REPO_ROOT = Path(__file__).resolve().parents[2]
CICD_DIR = REPO_ROOT / "cicd"
TF_CLOUDBUILD_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project" / "modules" / "cloudbuild"
SINGLE_PROJECT_DIR = REPO_ROOT / "deploy" / "terraform" / "single-project"


# REQ-CICD-001
def test_cicd_files_exist():
    """All three declarative Cloud Build specifications must exist."""
    assert (CICD_DIR / "cloudbuild.yaml").is_file(), "Missing cicd/cloudbuild.yaml"
    assert (CICD_DIR / "cloudbuild-reindex.yaml").is_file(), "Missing cicd/cloudbuild-reindex.yaml"
    assert (CICD_DIR / "cloudbuild-nightly.yaml").is_file(), "Missing cicd/cloudbuild-nightly.yaml"


# REQ-CICD-002
# REQ-CICD-009
def test_ci_pipeline_contract():
    """Main CI pipeline must execute tests, py_compile, corpus lint, terraform, and eval gate."""
    cb_path = CICD_DIR / "cloudbuild.yaml"
    raw_content = cb_path.read_text(encoding="utf-8")

    # Step IDs present
    assert "id: 'detect-changes'" in raw_content
    assert "id: 'unit-and-resilience-tests'" in raw_content
    assert "id: 'terraform-fmt-check'" in raw_content
    assert "id: 'terraform-validate'" in raw_content
    assert "id: 'terraform-tflint'" in raw_content
    assert "id: 'golden-eval-gate'" in raw_content

    # Change detection and evaluation substitution
    assert "_RUN_EVAL: 'auto'" in raw_content
    assert "gcr.io/google.com/cloudsdktool/cloud-sdk:slim" in raw_content
    assert "git fetch" not in raw_content
    assert 'gsutil cat "gs://${_CORPUS_BUCKET}/ci/state.json" > /workspace/ci_state_prev.json' in raw_content
    assert "--state-in /workspace/ci_state_prev.json" in raw_content
    assert "--state-out /workspace/ci_state_new.json" in raw_content

    # State recording final step
    assert "id: 'record-ci-state'" in raw_content
    assert "waitFor: ['post-deploy-smoke', 'corpus-reindex-on-main', 'terraform-tflint']" in raw_content
    assert 'if [ "${BRANCH_NAME}" != "main" ]; then' in raw_content
    assert 'gsutil cp /workspace/ci_state_new.json "gs://${_CORPUS_BUCKET}/ci/state.json"' in raw_content

    # Step commands and tools
    assert "ruff check rag/ tests/" in raw_content
    assert "python -m pytest -v" in raw_content
    assert "py_compile" in raw_content
    assert "publish_faq.py --work rag/corpus_v2 --lint-only" in raw_content
    assert "terraform fmt -check" in raw_content
    assert "ci_eval_gate.py" in raw_content
    assert "--min-recall 0.90" in raw_content
    assert "--min-mrr 0.75" in raw_content
    assert "--max-ndcg-drop 0.02" in raw_content

    # Logging option
    assert "logging: CLOUD_LOGGING_ONLY" in raw_content


# REQ-CICD-003
def test_reindex_pipeline_contract():
    """Reindex pipeline must lint, publish to Discovery Engine, eval, and check delta gate with rollback."""
    reindex_path = CICD_DIR / "cloudbuild-reindex.yaml"
    raw = reindex_path.read_text(encoding="utf-8")

    # Step IDs present
    assert "id: 'reindex-corpus-lint'" in raw
    assert "id: 'publish-faq-release'" in raw
    assert "id: 'fetch-eval-dataset'" in raw
    assert "id: 'run-retrieval-eval'" in raw
    assert "id: 'enforce-regression-gate'" in raw

    assert "publish_faq.py --work rag/corpus_v2 --lint-only" in raw
    assert "publish_faq.py" in raw
    assert "retrieval_eval.py" in raw
    assert "GATE_THRESHOLD = -0.02" in raw or "-0.02" in raw
    assert "rollback" in raw.lower()


# REQ-CICD-004
def test_nightly_pipeline_contract():
    """Nightly pipeline must evaluate DEV query set, enforce drift threshold, and archive to GCS."""
    nightly_path = CICD_DIR / "cloudbuild-nightly.yaml"
    raw = nightly_path.read_text(encoding="utf-8")

    assert "id: 'download-eval-and-baseline'" in raw
    assert "id: 'run-retrieval-health-eval'" in raw
    assert "id: 'archive-evaluation-results'" in raw

    assert "ci_eval_gate.py" in raw
    assert "eval/nightly/" in raw


# REQ-CICD-005
def test_cloudbuild_sa_least_privilege():
    """Build and scheduler SAs must have least-privilege roles without primitive roles."""
    main_tf = (TF_CLOUDBUILD_DIR / "main.tf").read_text(encoding="utf-8")

    assert "google_service_account" in main_tf
    assert "ticket-router-build" in main_tf
    assert "ticket-router-scheduler" in main_tf

    # Allowed role bindings
    assert "roles/discoveryengine.editor" in main_tf
    assert "roles/logging.logWriter" in main_tf
    assert "roles/storage.objectAdmin" in main_tf
    assert "roles/cloudbuild.builds.editor" in main_tf

    # No forbidden primitive roles
    assert "roles/owner" not in main_tf
    assert "roles/editor" not in main_tf
    assert "google_project_iam_binding" not in main_tf


# REQ-CICD-006
def test_github_connection_gating():
    """GitHub connection and repository resources must be gated by var.github_connection_ready."""
    main_tf = (TF_CLOUDBUILD_DIR / "main.tf").read_text(encoding="utf-8")
    vars_tf = (TF_CLOUDBUILD_DIR / "variables.tf").read_text(encoding="utf-8")

    assert "google_cloudbuildv2_connection" in main_tf
    assert "google_cloudbuildv2_repository" in main_tf
    assert "count    = var.github_connection_ready ? 1 : 0" in main_tf

    # Trigger gating
    assert "var.github_connection_ready ? 1 : 0" in main_tf

    # Default value of github_connection_ready must be false
    assert "variable \"github_connection_ready\"" in vars_tf
    assert "default     = false" in vars_tf


# REQ-CICD-006
# REQ-CICD-009
def test_ci_triggers_enabled_by_default():
    """CI triggers must be enabled by default and configure _RUN_EVAL substitution."""
    main_tf = (TF_CLOUDBUILD_DIR / "main.tf").read_text(encoding="utf-8")
    vars_tf = (TF_CLOUDBUILD_DIR / "variables.tf").read_text(encoding="utf-8")
    sp_vars_tf = (SINGLE_PROJECT_DIR / "variables.tf").read_text(encoding="utf-8")

    assert "variable \"enable_ci_triggers\"" in vars_tf
    assert "default     = true" in vars_tf
    assert "variable \"enable_ci_triggers\"" in sp_vars_tf
    assert "default     = true" in sp_vars_tf
    assert "disabled        = !var.enable_ci_triggers" in main_tf
    assert '_RUN_EVAL = "auto"' in main_tf


# REQ-CICD-009
def test_eval_gate_conditional_execution():
    """Golden eval gate must conditionally execute based on _RUN_EVAL and RETRIEVAL_CHANGED."""
    cb_path = CICD_DIR / "cloudbuild.yaml"
    raw_content = cb_path.read_text(encoding="utf-8")

    assert "_RUN_EVAL: 'auto'" in raw_content
    assert "if [ -f /workspace/changes.env ]; then . /workspace/changes.env; fi" in raw_content
    assert 'RUN_EVAL="${_RUN_EVAL}"' in raw_content
    assert 'if [ "$${RUN_EVAL}" = "false" ]; then' in raw_content
    assert "Skipping golden eval gate: disabled via _RUN_EVAL=false" in raw_content
    assert 'elif [ "$${RUN_EVAL}" = "auto" ]; then' in raw_content
    assert 'if [ "$${RETRIEVAL_CHANGED:-false}" != "true" ]; then' in raw_content
    assert (
        "Skipping golden eval gate: no retrieval-relevant changes (set _RUN_EVAL=true to force)"
        in raw_content
    )


# REQ-CICD-009
def test_no_unsupported_cloudbuild_substitution_fallbacks():
    """Cloud Build built-ins and substitutions must not use bash fallback syntax (${VAR:-...}).

    Cloud Build only substitutes the exact form ${VAR}; ${VAR:-default} is passed
    to bash unsubstituted, resulting in empty values for environment variables not exported.
    """
    pattern = re.compile(r"\$\{(BRANCH_NAME|PROJECT_ID|SHORT_SHA|COMMIT_SHA|_[A-Z_]+):-")

    for yaml_path in sorted(CICD_DIR.glob("*.yaml")):
        content = yaml_path.read_text(encoding="utf-8")
        assert "${BRANCH_NAME:-" not in content, (
            f"Found unsupported '${{BRANCH_NAME:-' in {yaml_path.name}"
        )
        match = pattern.search(content)
        assert not match, (
            f"Found unsupported substitution fallback '{match.group(0)}' in {yaml_path.name}"
        )


# REQ-CICD-007
def test_scheduler_job_target_contract():
    """Scheduler job must target nightly trigger :run endpoint via OAuth."""
    main_tf = (TF_CLOUDBUILD_DIR / "main.tf").read_text(encoding="utf-8")

    assert "google_cloud_scheduler_job" in main_tf
    assert "support-ticket-router-nightly-eval" in main_tf
    assert "http_target" in main_tf
    assert ":run" in main_tf
    assert "oauth_token" in main_tf
    assert "service_account_email = google_service_account.scheduler_sa.email" in main_tf


# REQ-CICD-008
def test_no_secrets_in_cicd_yamls():
    """CI/CD YAML files must not inline tokens or secrets, and must avoid --allow-unauthenticated."""
    for yaml_file in CICD_DIR.glob("*.yaml"):
        content = yaml_file.read_text(encoding="utf-8")
        assert "--allow-unauthenticated" not in content, (
            f"Found forbidden --allow-unauthenticated in {yaml_file.name}"
        )
        assert "ghp_" not in content, f"Possible GitHub token in {yaml_file.name}"
        assert "AIzaSy" not in content, f"Possible Google API key in {yaml_file.name}"
        assert "BEGIN PRIVATE KEY" not in content, f"Private key in {yaml_file.name}"


# REQ-CICD-002
# REQ-CICD-004
def test_ci_eval_gate_logic(tmp_path):
    """Verify ci_eval_gate.py computes chain_version, checks thresholds, and enforces gates."""
    # 1. Chain version is deterministic 12-char string
    cv1 = compute_chain_version()
    cv2 = compute_chain_version()
    assert len(cv1) == 12
    assert cv1 == cv2

    # 2. Gate check logic
    baseline = {"recall@5": 0.900, "mrr@10": 0.750, "ndcg@5": 0.650}

    # Passing case
    pass_metrics = {"recall@5": 0.920, "mrr@10": 0.780, "ndcg@5": 0.655}
    passed, details = execute_gate_check(pass_metrics, baseline)
    assert passed
    assert all("PASS" in d for d in details)

    # Failing recall case
    fail_metrics = {"recall@5": 0.880, "mrr@10": 0.780, "ndcg@5": 0.655}
    passed, details = execute_gate_check(fail_metrics, baseline)
    assert not passed
    assert any("FAIL" in d for d in details)

    # Regressing nDCG case (> 0.02 drop)
    regress_metrics = {"recall@5": 0.910, "mrr@10": 0.760, "ndcg@5": 0.620}
    passed, details = execute_gate_check(regress_metrics, baseline)
    assert not passed

    # 3. CLI execution with mock system-run
    run_file = tmp_path / "system_run.json"
    run_file.write_text(json.dumps({
        "system_top10": {
            "q1": ["doc-1", "doc-2"],
            "q2": ["doc-3", "doc-4"]
        }
    }))
    queries_file = tmp_path / "queries.json"
    queries_file.write_text(json.dumps([
        {"id": "q1", "text": "test query 1", "gold_doc": "doc-1"},
        {"id": "q2", "text": "test query 2", "gold_doc": "doc-3"}
    ]))
    out_file = tmp_path / "out.json"

    exit_code = main([
        "--system-run", str(run_file),
        "--queries", str(queries_file),
        "--out", str(out_file),
        "--min-recall", "0.90",
        "--min-mrr", "0.75",
        "--enforce-gate"
    ])
    assert exit_code == 0
    assert out_file.exists()
    saved = json.loads(out_file.read_text())
    assert saved["verdict"] == "PASS"
    assert saved["chain_version"] == cv1


# REQ-CICD-002
# REQ-CICD-004
def test_ci_eval_gate_live_search_branch(monkeypatch, tmp_path):
    """Verify live SearchClient integration uses real dev.jsonl schema (qid/query) and cleans text."""
    from rag.search_client import SearchResult
    import rag.ci_eval_gate as eval_gate_module

    received_queries = []

    class FakeSearchClient:
        def __init__(self, project=None, engine=None, location=None):
            self.project = project

        def search(self, query_text, threshold="LOWEST", page_size=10):
            received_queries.append(query_text)
            return SearchResult(
                document_ids=["doc-1", "doc-2"],
                latency_ms=12.5,
                total_size=2,
                filter_applied="status: ANY(\"active\")",
            )

    monkeypatch.setattr(eval_gate_module, "SearchClient", FakeSearchClient)

    # Real dev.jsonl row schema: qid, query, relevant, intent, split, thread_id
    dev_rows = [
        {
            "qid": "d000",
            "query": "Where is my order #12345? [ORDER]",
            "relevant": "doc-1",
            "intent": "order_status",
            "split": "dev",
            "thread_id": "784356",
        },
        {
            "qid": "d001",
            "query": "How do I return an item?",
            "relevant": "doc-2",
            "intent": "returns_refunds",
            "split": "dev",
            "thread_id": "992144",
        },
    ]

    queries_file = tmp_path / "dev.jsonl"
    queries_file.write_text("\n".join(json.dumps(r) for r in dev_rows) + "\n", encoding="utf-8")
    out_file = tmp_path / "out.json"

    exit_code = eval_gate_module.main([
        "--queries", str(queries_file),
        "--out", str(out_file),
        "--project", "test-project",
        "--engine", "test-engine",
        "--location", "global",
        "--min-recall", "0.50",
        "--min-mrr", "0.50",
        "--enforce-gate"
    ])
    assert exit_code == 0
    assert out_file.exists()
    saved = json.loads(out_file.read_text())
    assert saved["verdict"] == "PASS"

    # 1. Assert fake client receives non-empty, cleaned query text
    assert len(received_queries) == 2
    assert "Where is my order" in received_queries[0]
    assert "[ORDER]" not in received_queries[0]
    assert received_queries[1] == "How do I return an item?"

    # 2. Assert results are keyed by qid, not thread_id
    assert "d000" in saved["system_top10"]
    assert "d001" in saved["system_top10"]
    assert "784356" not in saved["system_top10"]
    assert "992144" not in saved["system_top10"]

    # 3. Assert evaluate_system_results receives and handles the qid keys
    metrics = eval_gate_module.evaluate_system_results(
        system_top10=saved["system_top10"],
        labels_c_path=None,
        queries=dev_rows,
        scope="MAIN",
        split="dev",
    )
    assert metrics["recall@5"] == 1.0
    assert metrics["n_evaluated"] == 2


# REQ-CICD-002
# REQ-CICD-004
def test_ci_eval_gate_empty_query_fails_exit_2(monkeypatch, tmp_path):
    """Empty query strings in queries file must produce counted warning and fail with exit code 2."""
    from rag.search_client import SearchResult
    import rag.ci_eval_gate as eval_gate_module

    class FakeSearchClient:
        def __init__(self, project=None, engine=None, location=None):
            pass

        def search(self, query_text, threshold="LOWEST", page_size=10):
            return SearchResult(
                document_ids=["doc-1"],
                latency_ms=5.0,
                total_size=1,
                filter_applied="",
            )

    monkeypatch.setattr(eval_gate_module, "SearchClient", FakeSearchClient)

    bad_rows = [
        {"qid": "d000", "query": "", "relevant": "doc-1", "thread_id": "111"},
        {"qid": "d001", "query": "valid query", "relevant": "doc-2", "thread_id": "222"},
    ]
    queries_file = tmp_path / "bad_queries.jsonl"
    queries_file.write_text("\n".join(json.dumps(r) for r in bad_rows) + "\n", encoding="utf-8")
    out_file = tmp_path / "out.json"

    exit_code = eval_gate_module.main([
        "--queries", str(queries_file),
        "--out", str(out_file),
        "--project", "test-project",
        "--engine", "test-engine",
        "--location", "global",
    ])
    assert exit_code == 2


# REQ-CICD-004
def test_ci_eval_gate_missing_inputs_returns_exit_2(tmp_path):
    """ci_eval_gate.py must exit with code 2 ('could not run') when inputs are missing."""
    # 1. Nonexistent queries file
    exit_code = main(["--queries", "/nonexistent/path/queries.jsonl"])
    assert exit_code == 2

    # 2. Nonexistent system_run file
    exit_code = main(["--system-run", "/nonexistent/path/system_run.json"])
    assert exit_code == 2

    # 3. Valid queries but nonexistent baseline
    queries_file = tmp_path / "valid_queries.json"
    queries_file.write_text(json.dumps([{"id": "q1", "text": "sample", "gold_doc": "doc-1"}]))
    run_file = tmp_path / "valid_run.json"
    run_file.write_text(json.dumps({"system_top10": {"q1": ["doc-1"]}}))

    exit_code = main([
        "--system-run", str(run_file),
        "--queries", str(queries_file),
        "--baseline", "/nonexistent/baseline.json",
    ])
    assert exit_code == 2

    # 4. Valid queries but nonexistent labels-c
    exit_code = main([
        "--system-run", str(run_file),
        "--queries", str(queries_file),
        "--labels-c", "/nonexistent/labels.jsonl",
    ])
    assert exit_code == 2


# REQ-CICD-004
def test_cloudbuild_eval_gate_fails_on_missing_data():
    """Cloud Build golden eval gate must not hide missing GCS eval data with || true or fallback files."""
    cloudbuild = (CICD_DIR / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert 'gsutil -m cp "gs://${_CORPUS_BUCKET}/eval/*" /workspace/eval/' in cloudbuild
    assert 'gsutil -m cp "gs://${_CORPUS_BUCKET}/eval/*" /workspace/eval/ || true' not in cloudbuild
    assert "creating fallback DEV set" not in cloudbuild

    reindex = (CICD_DIR / "cloudbuild-reindex.yaml").read_text(encoding="utf-8")
    assert "|| true" not in reindex
    assert "creating fallback DEV set" not in reindex

    nightly = (CICD_DIR / "cloudbuild-nightly.yaml").read_text(encoding="utf-8")
    assert "|| true" not in nightly
    assert "Creating fallback DEV set" not in nightly


# REQ-CICD-008
def test_cloudbuild_no_hardcoded_project_literal():
    """Embedded Python scripts in Cloud Build YAMLs must use $PROJECT_ID without literal fallback."""
    reindex = (CICD_DIR / "cloudbuild-reindex.yaml").read_text(encoding="utf-8")
    assert 'project = os.environ.get("PROJECT_ID", "susmith-fde-ticket-router")' not in reindex
    assert 'project = "${PROJECT_ID}"' in reindex


# REQ-CICD-004
def test_cloudbuild_labels_c2_preferred_over_labels_c():
    """Cloud Build pipelines must prioritize canonical labels_c2.jsonl before fallback labels_c.jsonl."""
    cloudbuild = (CICD_DIR / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert re.search(
        r"if \[ -f /workspace/eval/labels_c2\.jsonl \]; then\s+LABELS_ARG=\"--labels-c /workspace/eval/labels_c2\.jsonl\"\s+elif \[ -f /workspace/eval/labels_c\.jsonl \]; then",
        cloudbuild,
    ), "cloudbuild.yaml must check labels_c2.jsonl before labels_c.jsonl"

    nightly = (CICD_DIR / "cloudbuild-nightly.yaml").read_text(encoding="utf-8")
    assert re.search(
        r"if \[ -f /workspace/eval/labels_c2\.jsonl \]; then\s+LABELS_ARG=\"--labels-c /workspace/eval/labels_c2\.jsonl\"\s+elif \[ -f /workspace/eval/labels_c\.jsonl \]; then",
        nightly,
    ), "cloudbuild-nightly.yaml must check labels_c2.jsonl before labels_c.jsonl"


# REQ-CICD-002
# REQ-CICD-004
def test_ci_eval_gate_multi_label_scoring_regression(monkeypatch, tmp_path):
    """Regression test: multi_label_eval consensus metrics must correctly populate non-zero recall.

    Verifies end-to-end execution:
    1. Through main() with --system-run and --labels-c
    2. Through main() with live search branch (Fake SearchClient) and --labels-c
    """
    from rag.search_client import SearchResult
    import rag.ci_eval_gate as eval_gate_module

    # Real schemas: dev queries, C2 labels, system top-10
    dev_rows = [
        {
            "qid": "d000",
            "query": "Where is my order #12345? [ORDER]",
            "relevant": "faq-order-status-track",
            "intent": "order_status",
            "split": "dev",
            "thread_id": "784356",
        },
        {
            "qid": "d001",
            "query": "How do I return an item?",
            "relevant": "faq-returns-refunds-window",
            "intent": "returns_refunds",
            "split": "dev",
            "thread_id": "992144",
        },
    ]
    labels_c2_rows = [
        {
            "qid": "d000",
            "support_request": True,
            "language": "en",
            "primary": "faq-order-status-track",
            "relevant": ["faq-order-status-track", "faq-order-status-delay"],
            "note": "order tracking",
        },
        {
            "qid": "d001",
            "support_request": True,
            "language": "en",
            "primary": "faq-returns-refunds-window",
            "relevant": ["faq-returns-refunds-window"],
            "note": "return window",
        },
    ]

    dev_file = tmp_path / "dev.jsonl"
    dev_file.write_text("\n".join(json.dumps(r) for r in dev_rows) + "\n", encoding="utf-8")

    labels_file = tmp_path / "labels_c2.jsonl"
    labels_file.write_text("\n".join(json.dumps(r) for r in labels_c2_rows) + "\n", encoding="utf-8")

    system_run_file = tmp_path / "system_run.json"
    system_run_file.write_text(json.dumps({
        "system_top10": {
            "d000": ["faq-order-status-track", "faq-other-complaint"],
            "d001": ["faq-returns-refunds-window", "faq-other-complaint"],
        }
    }), encoding="utf-8")

    # 1. End-to-end through main() with --system-run and --labels-c
    out_file_sys = tmp_path / "out_sys.json"
    exit_code = eval_gate_module.main([
        "--system-run", str(system_run_file),
        "--queries", str(dev_file),
        "--labels-c", str(labels_file),
        "--out", str(out_file_sys),
        "--min-recall", "0.90",
        "--min-mrr", "0.75",
        "--enforce-gate",
    ])
    assert exit_code == 0
    assert out_file_sys.exists()
    saved_sys = json.loads(out_file_sys.read_text())
    assert saved_sys["verdict"] == "PASS"
    assert saved_sys["metrics"]["recall@5"] == 1.0
    assert saved_sys["metrics"]["mrr@10"] == 1.0
    assert saved_sys["metrics"]["n_evaluated"] == 2

    # 2. End-to-end through main() with live search branch and --labels-c
    class FakeSearchClient:
        def __init__(self, project=None, engine=None, location=None):
            pass

        def search(self, query_text, threshold="LOWEST", page_size=10):
            if "Where is my order" in query_text:
                return SearchResult(
                    document_ids=["faq-order-status-track"],
                    latency_ms=10.0,
                    total_size=1,
                    filter_applied="",
                )
            return SearchResult(
                document_ids=["faq-returns-refunds-window"],
                latency_ms=10.0,
                total_size=1,
                filter_applied="",
            )

    monkeypatch.setattr(eval_gate_module, "SearchClient", FakeSearchClient)

    out_file_live = tmp_path / "out_live.json"
    exit_code_live = eval_gate_module.main([
        "--queries", str(dev_file),
        "--labels-c", str(labels_file),
        "--out", str(out_file_live),
        "--project", "test-project",
        "--engine", "test-engine",
        "--location", "global",
        "--min-recall", "0.90",
        "--min-mrr", "0.75",
        "--enforce-gate",
    ])
    assert exit_code_live == 0
    assert out_file_live.exists()
    saved_live = json.loads(out_file_live.read_text())
    assert saved_live["verdict"] == "PASS"
    assert saved_live["metrics"]["recall@5"] == 1.0
    assert saved_live["metrics"]["mrr@10"] == 1.0
    assert saved_live["metrics"]["n_evaluated"] == 2
