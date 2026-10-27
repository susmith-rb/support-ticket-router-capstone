"""serving/laya/engine.py — Model execution engine abstraction for Laya router and reranker.

Provides clean architectural separation:
1. ModelEngine: Abstract base class for prediction engines.
2. FakeModelEngine: Zero-dependency mock engine for offline unit and contract tests.
3. LayaTorchEngine: Production PyTorch/Laya engine that loads checkpoints once onto GPU/CPU.
"""

import abc
import glob
import logging
import os
import pathlib
import subprocess
import time
from typing import Any, Dict, Optional

from serving.laya.policy import ROUTER_QUESTIONS

logger = logging.getLogger("serving.laya.engine")


class ModelEngine(abc.ABC):
    """Abstract model execution interface for router and choice reranker."""

    @abc.abstractmethod
    def predict_route(self, text: str) -> Dict[str, Any]:
        """Execute router-s1 multi-question forward pass on message text."""

    @abc.abstractmethod
    def predict_rerank(self, query: str, choice_question: Dict[str, Any]) -> Dict[str, float]:
        """Execute Laya choice model forward pass for query across candidate criteria."""

    @abc.abstractmethod
    def is_ready(self) -> bool:
        """Return True if models are loaded and warmed up."""

    @abc.abstractmethod
    def device(self) -> str:
        """Return execution device name (e.g. 'cuda', 'cuda:0', 'cpu')."""


class FakeModelEngine(ModelEngine):
    """Deterministic mock model engine for tests without PyTorch or GPU hardware."""

    def __init__(self, device: str = "cpu"):
        self._device = device
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    def device(self) -> str:
        return self._device

    def predict_route(self, text: str) -> Dict[str, Any]:
        """Generate deterministic pseudo-probabilities based on input keywords."""
        t_lower = text.lower()

        # Escalate heuristic
        is_angry_or_urgent = any(w in t_lower for w in ("broken", "angry", "furious", "lawyer", "refund", "stolen"))
        p_esc_true = 0.92 if is_angry_or_urgent else 0.04

        # Sentiment heuristic
        if "angry" in t_lower or "furious" in t_lower or "terrible" in t_lower:
            sent_probs = {"angry": 0.88, "frustrated": 0.08, "neutral": 0.03, "positive": 0.01}
        elif "frustrated" in t_lower or "broken" in t_lower or "delayed" in t_lower:
            sent_probs = {"angry": 0.12, "frustrated": 0.76, "neutral": 0.10, "positive": 0.02}
        elif "thank" in t_lower or "great" in t_lower:
            sent_probs = {"angry": 0.01, "frustrated": 0.02, "neutral": 0.15, "positive": 0.82}
        else:
            sent_probs = {"angry": 0.05, "frustrated": 0.10, "neutral": 0.80, "positive": 0.05}

        # Urgency heuristic
        if "immediate" in t_lower or "asap" in t_lower or "emergency" in t_lower:
            urg_probs = {"0": 0.01, "1": 0.04, "2": 0.15, "3": 0.80}
        elif "soon" in t_lower or "today" in t_lower:
            urg_probs = {"0": 0.05, "1": 0.15, "2": 0.70, "3": 0.10}
        else:
            urg_probs = {"0": 0.75, "1": 0.15, "2": 0.08, "3": 0.02}

        # Intent heuristic
        if any(w in t_lower for w in ("order", "track", "delivery", "arrived")):
            intent_probs = {"order_status": 0.85, "shipping_policy": 0.08, "returns_refunds": 0.04, "account_payment": 0.02, "other": 0.01}
        elif any(w in t_lower for w in ("ship", "international", "speed", "carrier")):
            intent_probs = {"order_status": 0.06, "shipping_policy": 0.84, "returns_refunds": 0.05, "account_payment": 0.03, "other": 0.02}
        elif any(w in t_lower for w in ("return", "refund", "exchange", "damaged")):
            intent_probs = {"order_status": 0.05, "shipping_policy": 0.04, "returns_refunds": 0.86, "account_payment": 0.03, "other": 0.02}
        elif any(w in t_lower for w in ("login", "password", "charge", "card", "payment")):
            intent_probs = {"order_status": 0.03, "shipping_policy": 0.02, "returns_refunds": 0.03, "account_payment": 0.90, "other": 0.02}
        else:
            intent_probs = {"order_status": 0.10, "shipping_policy": 0.10, "returns_refunds": 0.10, "account_payment": 0.10, "other": 0.60}

        return {
            "escalate": {"noul": p_esc_true, "probabilities": {"false": round(1.0 - p_esc_true, 4), "true": p_esc_true}},
            "sentiment": {"probabilities": sent_probs},
            "urgency": {"probabilities": urg_probs},
            "intent": {"probabilities": intent_probs},
        }

    def predict_rerank(self, query: str, choice_question: Dict[str, Any]) -> Dict[str, float]:
        """Generate deterministic choice probabilities for rerank candidates."""
        criteria = choice_question.get("criteria", {})
        if not criteria:
            return {}

        c_keys = list(criteria.keys())
        q_lower = query.lower()
        scored: Dict[str, float] = {}

        for k in c_keys:
            crit_text = str(criteria[k]).lower()
            overlap = sum(1 for word in q_lower.split() if len(word) > 3 and word in crit_text)
            scored[k] = 1.0 + overlap * 2.0

        total = sum(scored.values()) or 1.0
        return {k: round(v / total, 4) for k, v in scored.items()}


def query_nvidia_smi() -> str:
    """Best-effort probe of host GPU and driver version."""
    for bin_path in ("/usr/bin/nvidia-smi", "/usr/local/nvidia/bin/nvidia-smi", "nvidia-smi"):
        try:
            res = subprocess.run(
                [bin_path, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except (FileNotFoundError, PermissionError, subprocess.SubprocessError, OSError):
            continue
    return ""


def download_gcs_prefix(uri: str, local_dest: str) -> str:
    """Download GCS prefix to local directory if uri starts with gs://."""
    if not uri.startswith("gs://"):
        return uri

    target_dir = pathlib.Path(local_dest)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Check if directory already has checkpoint contents
    if (target_dir / "model.safetensors").is_file():
        logger.info("Found cached checkpoint in %s, skipping download", target_dir)
        return str(target_dir)

    bucket_name, _, prefix = uri[len("gs://"):].partition("/")
    prefix = prefix.strip("/")
    logger.info("Downloading GCS checkpoint from bucket %s, prefix %s to %s", bucket_name, prefix, target_dir)

    try:
        from google.cloud import storage
        client = storage.Client()
        blobs = list(client.list_blobs(bucket_name, prefix=prefix + "/" if prefix else None))
        if not blobs:
            # Try without trailing slash
            blobs = list(client.list_blobs(bucket_name, prefix=prefix))

        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            rel_path = blob.name[len(prefix):].lstrip("/") if prefix else blob.name
            dest_file = target_dir / rel_path
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(dest_file))
        logger.info("Successfully downloaded %d blobs to %s", len(blobs), target_dir)
    except Exception as e:
        logger.warning("google.cloud.storage download failed: %s; attempting gsutil fallback", e)
        ret = os.system(f"gsutil -m cp -r '{uri.rstrip('/')}/*' '{target_dir}/'")
        if ret != 0:
            raise RuntimeError(f"Failed to download checkpoint from {uri} to {target_dir}") from e

    return str(target_dir)


class LayaTorchEngine(ModelEngine):
    """Production PyTorch/Laya engine that loads models once onto GPU or CPU."""

    def __init__(
        self,
        router_ckpt: str,
        reranker_ckpt: str,
        device: Optional[str] = None,
    ):
        self.router_ckpt_uri = router_ckpt
        self.reranker_ckpt_uri = reranker_ckpt
        self._device = device
        self._router_agent: Any = None
        self._reranker_agent: Any = None
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    def device(self) -> str:
        return self._device or "unknown"

    def load_models(self) -> None:
        """Download checkpoints (if needed) and load Laya models into memory."""
        import torch
        import laya

        torch_ver = torch.__version__
        try:
            torch_cuda_ver = str(torch.version.cuda)
        except (AttributeError, Exception):
            torch_cuda_ver = "none"

        try:
            cuda_avail = bool(torch.cuda.is_available())
        except (AttributeError, Exception):
            cuda_avail = False

        device_name = "none"
        if cuda_avail:
            try:
                device_name = str(torch.cuda.get_device_name(0))
            except Exception as e:
                device_name = f"error: {e}"

        logger.info(
            "PyTorch Runtime Diagnostics: version=%s, cuda_version=%s, cuda_available=%s, device_name=%s",
            torch_ver,
            torch_cuda_ver,
            cuda_avail,
            device_name,
        )

        nv_visible_devices = os.environ.get("NVIDIA_VISIBLE_DEVICES", "<unset>")
        libcuda_matches = glob.glob("/usr/local/nvidia/lib64/libcuda.so*") + glob.glob(
            "/usr/lib/x86_64-linux-gnu/libcuda.so*"
        )
        logger.info(
            "Container NVIDIA Environment Diagnostics: NVIDIA_VISIBLE_DEVICES=%s, libcuda_matches=%s",
            nv_visible_devices,
            libcuda_matches,
        )

        nvidia_smi_output = query_nvidia_smi()
        if nvidia_smi_output:
            logger.info("Host NVIDIA Driver / GPU Info:\n%s", nvidia_smi_output)
        else:
            logger.info("nvidia-smi not available or returned no output")

        require_gpu = os.environ.get("REQUIRE_GPU", "1").lower() in ("1", "true", "yes")
        if require_gpu and not cuda_avail:
            logger.critical(
                "CRITICAL: CUDA acceleration is required (REQUIRE_GPU=%s) but torch.cuda.is_available() is False. "
                "PyTorch version: %s, compiled CUDA version: %s. Failing startup to prevent silent CPU fallback.",
                os.environ.get("REQUIRE_GPU", "1"),
                torch_ver,
                torch_cuda_ver,
            )
            raise RuntimeError(
                f"CUDA unavailable on host (torch {torch_ver} with CUDA {torch_cuda_ver}), but REQUIRE_GPU=1 is set. "
                "Aborting startup to prevent high-latency CPU serving SLA breach."
            )

        if not self._device:
            self._device = "cuda" if cuda_avail else "cpu"

        # Resolve local paths
        router_dir = download_gcs_prefix(self.router_ckpt_uri, "/tmp/router_s1_ckpt")
        reranker_dir = download_gcs_prefix(self.reranker_ckpt_uri, "/tmp/reranker_ckpt")

        logger.info("Loading router-s1 model from %s onto %s", router_dir, self._device)
        t0 = time.perf_counter()
        self._router_agent = laya.load(router_dir)
        router_load_s = time.perf_counter() - t0
        logger.info("Loaded router-s1 in %.2f s", router_load_s)

        logger.info("Loading Laya choice reranker from %s onto %s", reranker_dir, self._device)
        t0 = time.perf_counter()
        self._reranker_agent = laya.load(reranker_dir)
        rerank_load_s = time.perf_counter() - t0
        logger.info("Loaded reranker in %.2f s", rerank_load_s)

        # Warmup models
        logger.info("Warming up models...")
        self._router_agent.predict("warmup query", ROUTER_QUESTIONS)
        dummy_choice = {
            "type": "choice",
            "instructions": "Warmup",
            "criteria": {"d1": "Option 1", "d2": "Option 2"},
        }
        self._reranker_agent.predict("warmup query", {"article": dummy_choice})
        logger.info("Warmup complete. Serving ready on %s.", self._device)
        self._ready = True

    def predict_route(self, text: str) -> Dict[str, Any]:
        """Execute router-s1 inference pass."""
        if not self._ready or self._router_agent is None:
            raise RuntimeError("Router-s1 model is not loaded")
        resp = self._router_agent.predict(text, ROUTER_QUESTIONS)
        return resp.get("answers", resp)

    def predict_rerank(self, query: str, choice_question: Dict[str, Any]) -> Dict[str, float]:
        """Execute choice reranker inference pass."""
        if not self._ready or self._reranker_agent is None:
            raise RuntimeError("Reranker model is not loaded")
        resp = self._reranker_agent.predict(query, {"article": choice_question})
        answers = resp.get("answers", resp)
        art_ans = answers.get("article", {})
        probs = art_ans.get("choice_probabilities", art_ans.get("probabilities", {}))
        return {str(k): float(v) for k, v in probs.items()}
