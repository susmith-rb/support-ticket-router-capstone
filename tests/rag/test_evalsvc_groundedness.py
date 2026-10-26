import io
import json
import urllib.error
from evals.evalsvc_groundedness import aggregate_scores, build_request_body, evaluate_single_instance


# REQ-RAG-RETR-007
def test_build_request_body():
    prediction = "You can return within 30 days."
    context = "<doc>Returns are 30 days.</doc>"
    body = build_request_body(prediction, context, model="gemini-3.8-flash", project="test-project", loc="global")

    assert "pointwiseMetricInput" in body
    metric_input = body["pointwiseMetricInput"]
    assert "metricSpec" in metric_input
    assert "metricPromptTemplate" in metric_input["metricSpec"]
    instance = json.loads(metric_input["instance"]["jsonInstance"])
    assert instance["prediction"] == prediction
    assert instance["context"] == context

    assert "autoraterConfig" in body
    assert body["autoraterConfig"]["autoraterModel"] == "projects/test-project/locations/global/publishers/google/models/gemini-3.8-flash"


# REQ-RAG-RETR-007
def test_evaluate_single_instance_retry_429(monkeypatch):
    calls = []
    sleeps = []

    class DummyResponse:
        def read(self):
            return json.dumps({"pointwiseMetricResult": {"score": 5, "explanation": "fully grounded"}}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=300):
        calls.append(req)
        if len(calls) < 3:
            # raise HTTP 429 twice
            fp = io.BytesIO(b'{"error": "Resource exhausted"}')
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, fp)
        return DummyResponse()

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    res = evaluate_single_instance(
        url="http://mock-evalsvc",
        tok="fake-token",
        project="test-project",
        body={},
        question="Can I return this?",
        max_retries=5,
        sleep_fn=lambda s: sleeps.append(s),
    )

    assert len(calls) == 3
    assert len(sleeps) == 2
    assert res["score"] == 5
    assert res["explanation"] == "fully grounded"


# REQ-RAG-RETR-007
def test_evaluate_single_instance_timeout_retry(monkeypatch):
    calls = []
    sleeps = []

    class DummyResponse:
        def read(self):
            return json.dumps({"pointwiseMetricResult": {"score": 4, "explanation": "mostly grounded"}}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=300):
        calls.append(req)
        if len(calls) == 1:
            raise TimeoutError("Connection timed out")
        return DummyResponse()

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    res = evaluate_single_instance(
        url="http://mock-evalsvc",
        tok="fake-token",
        project="test-project",
        body={},
        question="How long for refunds?",
        max_retries=5,
        sleep_fn=lambda s: sleeps.append(s),
    )

    assert len(calls) == 2
    assert len(sleeps) == 1
    assert res["score"] == 4


# REQ-RAG-RETR-007
def test_evaluate_single_instance_error_exhausted(monkeypatch):
    calls = []

    def mock_urlopen(req, timeout=300):
        calls.append(req)
        fp = io.BytesIO(b'{"error": "Unavailable"}')
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, fp)

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    res = evaluate_single_instance(
        url="http://mock-evalsvc",
        tok="fake-token",
        project="test-project",
        body={},
        question="Help",
        max_retries=3,
        sleep_fn=lambda s: None,
    )

    assert len(calls) == 3
    assert "error" in res
    assert "503" in res["error"]


# REQ-RAG-RETR-007
def test_aggregate_scores():
    results = [
        {"question": "q1", "score": 5},
        {"question": "q2", "score": 5},
        {"question": "q3", "score": 4},
        {"question": "q4", "score": 3},
        {"question": "q5", "error": "timeout"},
    ]
    summary = aggregate_scores(results, model="gemini-3.8-flash")
    assert summary["scored"] == 4
    assert summary["errors"] == 1
    assert summary["mean_1to5"] == round((5 + 5 + 4 + 3) / 4, 2)  # 4.25
    assert summary["share_score_ge4"] == round(3 / 4, 3)  # 0.75
    assert summary["distribution"] == {3: 1, 4: 1, 5: 2}
    assert summary["first_error"] == "timeout"
    assert summary["autorater"] == "gemini-3.8-flash"
