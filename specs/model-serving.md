# Specification: Vertex AI Model Serving (`serving/laya`)

This specification defines the container architecture, HTTP contract, inference policies, and deployment topology for serving the fine-tuned Laya encoder models (`router-s1` v2 and the `choice reranker` v2) on Vertex AI Prediction endpoints.

---

## 1. Overview & Co-located Architecture

The Capstone routing and retrieval pipeline relies on two fine-tuned Laya encoder checkpoints:
1. **`router-s1` (v2)**: 4-head ModernBERT classification model evaluating customer inquiry urgency, sentiment, escalation need, and functional intent.
2. **`choice reranker` (v2)**: Choice-formatted cross-encoder evaluating semantic relevance between customer queries and retrieved knowledge-base chunks or suggested routing actions.

```
Incoming Request (:predict or :rawPredict)
                 │
                 ▼
       [FastAPI Serving Layer] (AIP_HTTP_PORT: 8080)
                 │
                 ├── Health Check: GET /healthz (200 OK when warm)
                 │
                 └── Predict Route: POST /predict
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
      task: "route"               task: "rerank"
             │                           │
  [Pre-inference Policy]                 │
  - Clean whitespace                     │
  - Chunk if > 1800 chars                │
  - Format 4 classification heads        │
             │                           │
  [router-s1 Model Head]                 │
  - Logits computation                  │
  - T=1.18 Temp scaling                  │
  - Chunk pooling (max/mean)             │
             │                           │
             │                [reranker Model Head]
             │                - Format choice question
             │                - Score candidates
             │                - Sort descending by score
             │                           │
             └─────────────┬─────────────┘
                           │
                           ▼
              Response + Server-Timing Header
```

### Shared GPU Colocation Decision
Both models are based on ModernBERT (~421M parameters, ~842 MB FP32 weights each). Co-locating both models in a single container on a single NVIDIA L4 GPU (24 GB VRAM) requires under 2.5 GB of GPU memory, leaving >21 GB for dynamic batching and activations. This avoids the cost and cold-start latency of running two separate GPU endpoints.

---

## 2. Vertex AI Custom Container Contract

The serving container conforms to the Vertex AI custom container specification:

- **Port Configuration**: Binds to `0.0.0.0:${AIP_HTTP_PORT:-8080}`.
- **Health Endpoint**: `${AIP_HEALTH_ROUTE:-/healthz}` returning HTTP 200 `{"status": "healthy", "router_loaded": true, "reranker_loaded": true}` once warm.
- **Prediction Endpoint**: `${AIP_PREDICT_ROUTE:-/predict}` accepting `{"instances": [...]}`.
- **Observability**: Returns `Server-Timing: total;dur=..., inference;dur=...` headers for downstream tracing and latency monitoring.

### Runtime Environment & `laya-tune` Parity
To eliminate PyTorch/Transformers runtime compatibility defects (where recent `transformers` versions disable PyTorch and fail on `from transformers import PreTrainedModel` with `NameError: name 'nn' is not defined`), the serving container strictly pins dependencies to match the validated `laya-tune` serving venv:
- Base: Python 3.12 (`python:3.12-slim`)
- PyTorch: `torch==2.14.1+cu126` installed in a dedicated step using `--index-url https://download.pytorch.org/whl/cu126` to guarantee CUDA 12.6 driver compatibility on Vertex GPU nodes (T4/L4) and prevent pip from pulling PyPI CUDA 13 builds.
- Remaining dependencies in `serving/laya/requirements.txt`:
  - `transformers==5.20.2`
  - `laya==0.3.20`
  - `tokenizers==0.23.3`
  - `safetensors==0.8.0`
  - `huggingface_hub==2.2.0`
  - `numpy==2.5.4`
  - Serving web stack: `fastapi==0.115.6`, `uvicorn[standard]==0.34.0`, `pydantic==2.10.4`, `google-cloud-storage==2.19.0`, `requests==2.32.3`

### NVIDIA Container Runtime Environment (Host Driver Injection)
Standard Python base images (`python:3.12-slim`), unlike `nvidia/cuda` or `pytorch/pytorch`, do not include default NVIDIA container toolkit environment variables. Vertex AI Prediction and GKE nodes inject host GPU driver binaries and libraries into `/usr/local/nvidia/bin` and `/usr/local/nvidia/lib64`. For the container runtime to perform driver mounting and expose the GPU hardware, the `Dockerfile` explicitly declares:
- `NVIDIA_VISIBLE_DEVICES=all`
- `NVIDIA_DRIVER_CAPABILITIES=compute,utility`
- `LD_LIBRARY_PATH=/usr/local/nvidia/lib64:/usr/local/nvidia/lib:${LD_LIBRARY_PATH}`
- `PATH=/usr/local/nvidia/bin:${PATH}`

Startup diagnostics in `serving/laya/engine.py` log `NVIDIA_VISIBLE_DEVICES` and glob for host `libcuda.so*` files to provide instant observability into driver injection.

**Build-Time Sanity Verification**: The `Dockerfile` includes an explicit verification step that runs during image build:
`RUN python -c "import torch, transformers, laya; assert torch.__version__.startswith('2.14'), torch.__version__; assert torch.version.cuda and torch.version.cuda.startswith('12'), f'Expected CUDA 12, got {torch.version.cuda}'; from transformers import PreTrainedModel; print(f'Torch: {torch.__version__}, CUDA: {torch.version.cuda}, Transformers: {transformers.__version__}, Laya: {laya.__version__}')"`
This ensures package compatibility and CUDA 12 runtime targeting are asserted at container build time, failing fast instead of incurring 25-minute deployment failure cycles.

### GPU Required Invariant (No Silent CPU Fallback)
Interactive SLA budgets (<150 ms) require hardware GPU acceleration. Under CPU serving, router p95 latency spikes to ~3,106 ms and reranker p95 spikes to ~1,482 ms. To strictly enforce this invariant:
- The serving container defaults `REQUIRE_GPU=1`.
- On startup, the engine queries runtime diagnostics (`torch.__version__`, `torch.version.cuda`, `torch.cuda.is_available()`, and host driver info via `nvidia-smi`).
- If `REQUIRE_GPU=1` and `torch.cuda.is_available()` is False, the service logs `CRITICAL` and raises `RuntimeError`, causing container startup to fail fast instead of silently serving traffic on CPU and violating SLA budgets.

---

## 3. Pre-Inference & Post-Inference Policies

### Long-Input Handling & Chunk Aggregation
For texts exceeding 1800 characters (exceeding token capacity for low-latency classification):
1. Normalizes unicode spaces, tabs, and trims excessive consecutive newlines.
2. Splits into overlapping text chunks of 1500 characters with 300 character overlap.
3. Computes multi-head probabilities per chunk with temperature scaling ($T = 1.18$).
4. Aggregates predictions across chunks:
   - **`escalate`**: Max pooling ($\max_i P(\text{escalate}_i)$) to preserve customer safety and high recall.
   - **`urgency`**: Max pooling prioritizing `immediate` > `soon` > `normal`.
   - **`sentiment`**: Mean probability pooling across chunks.
   - **`intent`**: Mean probability pooling across chunks.

### Choice Reranker Format & Latency Observability
Formats candidates as choice options:
`Question: Which article best answers: "<query>"? Candidates: [A] <c0> [B] <c1> ...`
Extracts log probabilities for candidate tokens and produces normalized scores, sorted descending.

**Per-Instance Server Latency Parity**:
To provide latency observability parity with `route` decisions (which return `latency_ms` measuring in-process encoder forward pass and chunk pooling), choice reranker predictions include per-instance `latency_ms` on each candidate dictionary (`[{"id": "...", "score": ..., "latency_ms": 18.42}, ...]`). The returned list is also accessible via `pred["latency_ms"]` or `pred.latency_ms` in-process, preserving full backward-compatibility for clients iterating over candidate lists.

---

## 4. Hardware Sizing & Autoscaling Topology

- **Machine Type**: `g2-standard-4` (4 vCPU, 16 GB RAM) with 1× `NVIDIA_L4` GPU (or `n1-standard-4` with 1× `NVIDIA_TESLA_T4` fallback).
- **Replica Scaling**:
  - `MIN_REPLICAS = 1` (avoids cold start, maintains <150 ms latency).
  - `MAX_REPLICAS = 4` (default capstone autoscaling limit, overridable to 8 for load tests).
  - `AUTOSCALING_METRIC_TARGET = 60` (60% utilization target for CPU/GPU).
- **Service Account Permissions**:
  - `roles/storage.objectViewer` (to download model checkpoints from GCS).
  - `roles/logging.logWriter` (for Cloud Logging).
  - `roles/monitoring.metricWriter` (for Cloud Monitoring).

### Client Connection Pooling & In-Region Measured Latency
In-region client measurements from `laya-tune` (150 requests):
- **Unpooled HTTPS (urllib)**: Shared endpoint p95 ~185 ms, dedicated endpoint p95 ~185 ms due to repeated TLS handshake overhead.
- **Persistent Keep-Alive HTTPS (requests.Session + HTTPAdapter)**:
  - Shared endpoint: p50 96 ms, p95 134 ms.
  - Dedicated endpoint: p50 54 ms, p95 77 ms, p99 135 ms.
Therefore, clients MUST maintain persistent pooled keep-alive connections (`requests.Session` with `HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)`). Internal client timeouts are configured to 500 ms for route and 600 ms for rerank.

### Deployment Lifecycle: Traffic Split & Undeploy (`deploy.sh`)
When rolling out updated serving containers to a Vertex AI Endpoint:
- **100% Traffic Cutover**: Deploying a second model passes `--traffic-split=0=100` on `gcloud ai endpoints deploy-model` (where `0` targets the model being deployed), preventing the endpoint from remaining 100% routed to the previous model.
- **Previous Model Undeployment (`UNDEPLOY_PREVIOUS=1`)**: When enabled (default `0`), `deploy.sh` first updates the endpoint trafficSplit map to strictly 100% on the new deployed model ID (since Vertex AI rejects undeploy requests if an ID is present in the trafficSplit map, even with 0% weight), then calls `gcloud ai endpoints undeploy-model` on each superseded model ID, and finally outputs the endpoint's active deployedModels and trafficSplit state.

---

## 5. Requirements

### REQ-SERVE-001: Unified Vertex Custom Container Contract
- **Statement**: The model serving layer MUST provide a custom Docker container conforming to the Vertex AI custom container HTTP contract, binding to `AIP_HTTP_PORT` (default 8080) and handling `AIP_HEALTH_ROUTE` (`/healthz`) and `AIP_PREDICT_ROUTE` (`/predict`) with task dispatch for routing (`task: "route"`) and reranking (`task: "rerank"`).
- **Rationale**: Prebuilt Vertex Model Garden containers only support single-checkpoint Jev protocol; unified hosting requires custom container dispatch and Server-Timing observability (TDD §4.1, §5.4).
- **Acceptance**: `pytest tests/serving/test_server_contract.py`
- **Status**: done
- **Owner**: serving

### REQ-SERVE-002: Router Long-Input Policy and Chunk Aggregation
- **Statement**: Customer inquiry text exceeding 1800 characters MUST be normalized and split into 1500-character chunks with 300-character overlap, with predictions aggregated via max pooling for safety/urgency dimensions and mean pooling for sentiment/intent dimensions.
- **Rationale**: Long support tickets exceed single-window encoder token limits; pooling ensures high-severity paragraphs are not diluted by surrounding explanatory text (TDD §5.2).
- **Acceptance**: `pytest tests/serving/test_router_policy.py -k chunk`
- **Status**: done
- **Owner**: serving

### REQ-SERVE-003: Router Temperature Scaling and Decision Taxonomy
- **Statement**: The router classification heads MUST evaluate four typed dimensions (`escalate`, `sentiment`, `urgency`, `intent`) and calibrate raw logits using temperature scaling ($T = 1.18$) before applying softmax.
- **Rationale**: Empirical temperature scaling achieves calibration ECE $\le 0.08$, ensuring reliable confidence estimates for tiered threshold routing (TDD §5.3, training/router_s1/README.md).
- **Acceptance**: `pytest tests/serving/test_router_policy.py -k temperature`
- **Status**: done
- **Owner**: serving

### REQ-SERVE-004: Choice Reranker Candidate Scoring and Sorting
- **Statement**: The serving layer MUST support choice reranking requests by formatting candidate options into choice prompts, scoring each candidate token, and returning ranked candidates sorted descending by score.
- **Rationale**: Cross-encoder choice reranking refines top-k retrieval results for the RAG agent and knowledge-base search (TDD §7.2).
- **Acceptance**: `pytest tests/serving/test_router_policy.py -k rerank`
- **Status**: done
- **Owner**: serving

### REQ-SERVE-005: Shared GPU Replicas and Vertex Endpoint Infrastructure
- **Statement**: The Terraform module `router-endpoint` MUST provision a dedicated Vertex AI Prediction endpoint configured for shared GPU hosting with `min_replica_count = 1`, autoscaling `max_replica_count = 4` (utilization target 60% via `gpu-duty-cycle`), dedicated service account `ticket-router-serving`, and explicit impersonation bindings (`roles/iam.serviceAccountUser`) for the Vertex AI Service Agent and Cloud Build pipeline SA.
- **Rationale**: Colocating both ModernBERT models on an L4 GPU requires <2.5 GB VRAM, achieving sub-100 ms p50 latency while autoscaling dynamically for load bursts without cold start delays, with least-privilege IAM allowing seamless deployment and execution without keys.
- **Acceptance**: `pytest tests/infra/test_router_endpoint_static.py`
- **Status**: done
- **Owner**: serving

### REQ-SERVE-006: Inference Latency Budget and Smoke Verification
- **Statement**: The serving package MUST provide a standalone smoke verification client (`serving/laya/smoke.py`) measuring end-to-end endpoint latency against the 150 ms SLA budget across health check, routing, and reranking payloads.
- **Rationale**: Verifies deployment readiness and ensures production performance satisfies TDD §2.3 latency constraints.
- **Acceptance**: `pytest tests/infra/test_router_endpoint_static.py -k smoke`
- **Status**: done
- **Owner**: serving
