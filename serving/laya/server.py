"""serving/laya/server.py — FastAPI serving application conforming to Vertex AI custom container contract.

Exposes:
- GET /health (AIP_HEALTH_ROUTE): HTTP 200 when ready, HTTP 503 during cold load.
- POST /predict (AIP_PREDICT_ROUTE): Unified predict route for:
    1. {"task": "route", "text": "..."} -> System-1 router decision
    2. {"task": "rerank", "query": "...", "candidates": [...]} -> Choice reranker scores
"""

import contextlib
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from serving.laya.engine import FakeModelEngine, LayaTorchEngine, ModelEngine
from serving.laya.models import HealthResponse
from serving.laya.policy import (
    DEFAULT_ROUTER_TEMPERATURE,
    aggregate_chunk_decisions,
    build_rerank_choice_question,
    chunk_text,
    clean_text,
    format_rerank_response,
    format_single_chunk_decision,
)

try:
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import JSONResponse

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    Request = Any
    Response = Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("serving.laya.server")

# Default environment configuration per Vertex AI contract
DEFAULT_ROUTER_CKPT = "gs://susmith-fde-ticket-router-laya/checkpoints/router-s1/v2/"
DEFAULT_RERANKER_CKPT = "gs://susmith-fde-ticket-router-laya/rerank/v2/ckpt/"
MODEL_VERSION = "v2"


def get_configured_engine() -> ModelEngine:
    """Instantiate model engine based on environment configuration."""
    use_fake = os.environ.get("USE_FAKE_ENGINE", "false").lower() in ("1", "true", "yes")
    if use_fake:
        logger.info("Initializing FakeModelEngine for offline testing / mock serving")
        return FakeModelEngine(device="cpu")

    router_uri = os.environ.get("ROUTER_CKPT_URI", DEFAULT_ROUTER_CKPT)
    reranker_uri = os.environ.get("RERANKER_CKPT_URI", DEFAULT_RERANKER_CKPT)
    return LayaTorchEngine(router_ckpt=router_uri, reranker_ckpt=reranker_uri)


def process_predict_payload(
    request_data: Dict[str, Any],
    engine: ModelEngine,
) -> Tuple[Dict[str, Any], float]:
    """Pure-Python prediction processing logic for batch instances.

    Separated from framework HTTP handlers to enable direct hermetic testing
    without external networking or ASGI dependencies.
    """
    if not engine.is_ready():
        raise RuntimeError("Models are initializing. Service unavailable.")

    t0 = time.perf_counter()
    instances = request_data.get("instances")

    if instances is None:
        # Fallback for raw Jev body format or single instance
        if "state" in request_data:
            state = request_data["state"]
            text = state if isinstance(state, str) else state.get("body", str(state))
            instances = [{"task": "route", "text": text}]
        elif "task" in request_data:
            instances = [request_data]
        else:
            raise ValueError("Invalid request format: 'instances' array required")

    if not isinstance(instances, list) or len(instances) == 0:
        raise ValueError("'instances' must be a non-empty list")

    parameters = request_data.get("parameters", {}) or {}
    temp_override = parameters.get("temperature")
    temperature = float(temp_override) if temp_override is not None else DEFAULT_ROUTER_TEMPERATURE

    predictions: List[Any] = []

    for idx, inst in enumerate(instances):
        if not isinstance(inst, dict):
            raise ValueError(f"Instance at index {idx} must be a dictionary")

        task = inst.get("task", "route").lower()

        if task == "route":
            # --- Task: Router-s1 ---
            raw_text = inst.get("text", "")
            text = clean_text(raw_text)

            t_inst0 = time.perf_counter()
            chunks = chunk_text(text)
            chunk_decisions = []

            for chunk in chunks:
                raw_answers = engine.predict_route(chunk)
                chunk_dec = format_single_chunk_decision(raw_answers, temperature=temperature)
                chunk_decisions.append(chunk_dec)

            inst_latency_ms = (time.perf_counter() - t_inst0) * 1000.0
            decision = aggregate_chunk_decisions(
                chunk_decisions,
                model_version=MODEL_VERSION,
                latency_ms=inst_latency_ms,
            )
            predictions.append(decision)

        elif task == "rerank":
            # --- Task: Choice Reranker ---
            t_inst0 = time.perf_counter()
            query = clean_text(inst.get("query", ""))
            candidates = inst.get("candidates", [])
            if not isinstance(candidates, list) or len(candidates) == 0:
                raise ValueError(f"Instance {idx}: 'candidates' list cannot be empty")

            cand_ids = [str(c.get("id", f"doc_{i}")) for i, c in enumerate(candidates[:5])]
            cand_texts = {str(c.get("id", f"doc_{i}")): str(c.get("text", "")) for i, c in enumerate(candidates[:5])}
            choice_q = build_rerank_choice_question(cand_ids, cand_texts)

            choice_probs = engine.predict_rerank(query, choice_q)
            inst_latency_ms = (time.perf_counter() - t_inst0) * 1000.0
            ranked_candidates = format_rerank_response(
                candidates,
                choice_probs,
                latency_ms=inst_latency_ms,
            )
            predictions.append(ranked_candidates)

        else:
            raise ValueError(
                f"Instance {idx}: Unsupported task '{task}'. Supported tasks: 'route', 'rerank'."
            )

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return {"predictions": predictions}, elapsed_ms


@contextlib.asynccontextmanager
async def app_lifespan(app: Any):
    """Lifespan context manager handling model warmup."""
    logger.info("Starting up Laya serving container...")
    active_engine = app.state.engine
    if isinstance(active_engine, LayaTorchEngine):
        active_engine.load_models()
    logger.info("Laya serving container is READY on device %s.", active_engine.device())
    yield
    logger.info("Shutting down Laya serving container.")


async def handle_health_check(request: Request) -> Any:
    """Vertex AI liveness/readiness probe."""
    eng: ModelEngine = request.app.state.engine
    if not eng.is_ready():
        return JSONResponse(
            status_code=503,
            content={
                "status": "INITIALIZING",
                "router_loaded": False,
                "reranker_loaded": False,
                "device": eng.device(),
                "model_version": MODEL_VERSION,
                "uptime_seconds": round(time.time() - request.app.state.start_time, 2),
            },
        )
    return HealthResponse(
        status="HEALTHY",
        router_loaded=True,
        reranker_loaded=True,
        device=eng.device(),
        model_version=MODEL_VERSION,
        uptime_seconds=round(time.time() - request.app.state.start_time, 2),
    )


async def handle_predict(request_data: Dict[str, Any], response: Response, request: Request) -> Any:
    """Unified prediction route accepting batch instances."""
    eng: ModelEngine = request.app.state.engine
    try:
        result, elapsed_ms = process_predict_payload(request_data, eng)
        response.headers["Server-Timing"] = f"total;dur={elapsed_ms:.2f}"
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def create_app(engine: Optional[ModelEngine] = None) -> Any:
    """Create and configure the FastAPI application if FastAPI is installed."""
    if not FASTAPI_AVAILABLE:
        logger.warning("FastAPI not installed in current Python environment. Skipping create_app.")
        return None

    active_engine = engine or get_configured_engine()
    start_time = time.time()

    app = FastAPI(
        title="Laya Router & Reranker Serving Service",
        description="Unified Vertex AI Prediction container for router-s1 and Laya choice reranker",
        version="2.0.0",
        lifespan=app_lifespan,
    )

    app.state.engine = active_engine
    app.state.start_time = start_time

    health_route = os.environ.get("AIP_HEALTH_ROUTE", "/health")
    predict_route = os.environ.get("AIP_PREDICT_ROUTE", "/predict")

    app.add_api_route(health_route, handle_health_check, methods=["GET"], response_model=HealthResponse)
    if health_route != "/health":
        app.add_api_route("/health", handle_health_check, methods=["GET"], response_model=HealthResponse)

    app.add_api_route(predict_route, handle_predict, methods=["POST"])
    if predict_route != "/predict":
        app.add_api_route("/predict", handle_predict, methods=["POST"])
    app.add_api_route("/v1/systemone", handle_predict, methods=["POST"])

    return app


# Default app instance for ASGI servers if FastAPI is present
app = create_app() if FASTAPI_AVAILABLE else None


def main():
    """Run uvicorn server directly with AIP_HTTP_PORT."""
    import uvicorn

    port = int(os.environ.get("AIP_HTTP_PORT", os.environ.get("PORT", "8080")))
    uvicorn.run("serving.laya.server:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
