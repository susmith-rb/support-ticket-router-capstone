# Python 3.12 slim base with PyTorch CUDA 12 runtime for Vertex AI GPU Prediction
FROM python:3.12-slim

# Set environment flags
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIP_HTTP_PORT=8080 \
    AIP_HEALTH_ROUTE=/health \
    AIP_PREDICT_ROUTE=/predict \
    REQUIRE_GPU=1 \
    ROUTER_CKPT_URI=gs://susmith-fde-ticket-router-laya/checkpoints/router-s1/v2/ \
    RERANKER_CKPT_URI=gs://susmith-fde-ticket-router-laya/rerank/v2/ckpt/ \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/usr/local/nvidia/lib64:/usr/local/nvidia/lib:${LD_LIBRARY_PATH} \
    PATH=/usr/local/nvidia/bin:${PATH}

WORKDIR /app

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch with CUDA 12.6 support from the dedicated PyTorch wheel index
# (PyPI defaults to CUDA 13, which is incompatible with Vertex T4 node drivers)
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cu126 \
    "torch==2.14.1+cu126"

# Copy pinned requirements (matching laya-tune serving venv)
COPY serving/laya/requirements.txt /app/requirements.txt

# Install pinned Python packages
RUN pip install --no-cache-dir -r /app/requirements.txt

# Build-time sanity verification: fail image build immediately if PyTorch/CUDA 12/transformers/laya incompatibilities exist
RUN python -c "import torch, transformers, laya; assert torch.__version__.startswith('2.14'), torch.__version__; assert torch.version.cuda and torch.version.cuda.startswith('12'), f'Expected CUDA 12, got {torch.version.cuda}'; from transformers import PreTrainedModel; print(f'Torch: {torch.__version__}, CUDA: {torch.version.cuda}, Transformers: {transformers.__version__}, Laya: {laya.__version__}')"

# Create non-root application user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser && \
    mkdir -p /tmp/router_s1_ckpt /tmp/reranker_ckpt && \
    chown -R appuser:appgroup /app /tmp/router_s1_ckpt /tmp/reranker_ckpt

# Copy application source
COPY serving/ /app/serving/

USER appuser

EXPOSE 8080

# Run uvicorn server conforming to Vertex AI contract
CMD ["uvicorn", "serving.laya.server:app", "--host", "0.0.0.0", "--port", "8080"]
