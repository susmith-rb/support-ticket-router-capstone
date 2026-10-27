"""serving/laya/models.py — Request, response, and instance models for Vertex serving.

Complies with the Vertex AI custom prediction container contract:
- Request payload: `{"instances": [...], "parameters": {...}}`
- Response payload: `{"predictions": [...]}`
- Health check payload: `{"status": "HEALTHY", ...}`

Provides standard Pydantic models when pydantic is available, with clean fallback
for environments running unit tests without third-party dependencies.
"""

from typing import Any, Dict, List, Optional

try:
    from pydantic import BaseModel, Field

    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False

    class BaseModel:  # type: ignore[no-redef]
        """Fallback BaseModel for environments without Pydantic."""

        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

        def dict(self) -> Dict[str, Any]:
            return self.__dict__

        def model_dump(self) -> Dict[str, Any]:
            return self.__dict__

    def Field(default: Any = ..., **kwargs: Any) -> Any:  # noqa: N802
        return default


class RerankCandidate(BaseModel):
    """Candidate FAQ article for choice reranking."""

    id: str = Field(..., description="Document identifier (e.g. FAQ article doc_id).")
    text: str = Field(..., description="Article title and summary text.")


class RouteInstance(BaseModel):
    """System-1 router prediction instance."""

    task: str = Field(default="route", description="Inference task discriminator ('route').")
    text: str = Field(..., description="Inbound customer message or ticket body.")


class RerankInstance(BaseModel):
    """Choice reranker prediction instance."""

    task: str = Field(default="rerank", description="Inference task discriminator ('rerank').")
    query: str = Field(..., description="Customer inquiry or search query.")
    candidates: List[RerankCandidate] = Field(
        ...,
        description="Candidate articles to rerank (top-5 choice ceiling evaluated).",
    )


class PredictRequest(BaseModel):
    """Vertex AI prediction request body."""

    instances: List[Dict[str, Any]] = Field(
        ...,
        description="List of prediction instances (batch size up to 64).",
    )
    parameters: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional global prediction parameters (e.g. temperature override).",
    )


class PredictResponse(BaseModel):
    """Vertex AI prediction response body."""

    predictions: List[Any] = Field(
        ...,
        description="List of predictions corresponding to input instances.",
    )


class HealthResponse(BaseModel):
    """Container health check response."""

    status: str = Field(default="HEALTHY", description="Container readiness state.")
    router_loaded: bool = Field(default=False, description="Router checkpoint loaded.")
    reranker_loaded: bool = Field(default=False, description="Reranker checkpoint loaded.")
    device: str = Field(default="cpu", description="Execution device (cuda or cpu).")
    model_version: str = Field(default="v2", description="Serving model version.")
    uptime_seconds: float = Field(default=0.0, description="Process uptime in seconds.")
