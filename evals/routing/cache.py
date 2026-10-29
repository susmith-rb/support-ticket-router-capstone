"""Deterministic tier output caching for routing evaluation harness.

# REQ-REVAL-011: Runner targets, tier cache, dry-run & cost cap
Provides TierCache (file-backed SQLite) and caching wrappers:
- CachingS1Client
- CachingTier2Invoker
- CachingModelArmorClient (optional Model Armor caching)
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticket_router.models.router_s1_client import RouterPrediction

# Context-local call log for thread-safe concurrent per-case telemetry capture
_CURRENT_CALL_LOG: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar(
    "current_case_call_log", default=None
)


def get_current_call_log() -> Optional[List[Dict[str, Any]]]:
    """Retrieve the call log list for the active thread/case context."""
    return _CURRENT_CALL_LOG.get()


def set_current_call_log(log_list: Optional[List[Dict[str, Any]]]) -> contextvars.Token:
    """Set the call log list for the active thread/case context."""
    return _CURRENT_CALL_LOG.set(log_list)


class CacheMiss(BaseException):
    """Raised when tier output is requested in replay mode but not found in cache."""

    def __init__(self, key: Tuple[str, str, str, Optional[str], str, int], message: str = "") -> None:
        self.key = key
        msg = message or f"Cache miss in replay mode for key: {key}"
        super().__init__(msg)


def compute_input_sha256(text: str) -> str:
    """Compute deterministic SHA-256 hash of inbound ticket text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prediction_to_dict(pred: RouterPrediction) -> Dict[str, Any]:
    """Serialize RouterPrediction into a JSON-serializable dictionary."""
    return {
        "escalate": bool(pred.escalate),
        "escalate_prob": float(pred.escalate_prob),
        "sentiment": str(pred.sentiment),
        "sentiment_prob": float(pred.sentiment_prob),
        "urgency": str(pred.urgency),
        "urgency_prob": float(pred.urgency_prob),
        "intent": str(pred.intent),
        "intent_prob": float(pred.intent_prob),
        "probabilities": dict(pred.probabilities) if pred.probabilities else {},
        "latency_ms": float(pred.latency_ms),
        "raw": dict(pred.raw) if pred.raw else {},
        "out_of_scope": bool(pred.out_of_scope),
    }


def prediction_from_dict(d: Dict[str, Any]) -> RouterPrediction:
    """Reconstruct a RouterPrediction instance faithfully from dictionary."""
    return RouterPrediction(
        escalate=bool(d.get("escalate", False)),
        escalate_prob=float(d.get("escalate_prob", 0.0)),
        sentiment=str(d.get("sentiment", "neutral")),
        sentiment_prob=float(d.get("sentiment_prob", 0.0)),
        urgency=str(d.get("urgency", "normal")),
        urgency_prob=float(d.get("urgency_prob", 0.0)),
        intent=str(d.get("intent", "other")),
        intent_prob=float(d.get("intent_prob", 0.0)),
        probabilities=dict(d.get("probabilities") or {}),
        latency_ms=float(d.get("latency_ms", 0.0)),
        raw=dict(d.get("raw") or {}),
        out_of_scope=bool(d.get("out_of_scope", False)),
    )


class TierCache:
    """Thread-safe SQLite-backed tier prediction cache.

    Cache key = (tier, model_role, model_id, prompt_version, input_sha256, pass_index).
    """

    def __init__(self, root: Path | str = Path(".cache/routing_eval/tier_cache")) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "tier_cache.sqlite3"
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tier_cache (
                    cache_key TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    model_role TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    prompt_version TEXT,
                    input_sha256 TEXT NOT NULL,
                    pass_index INTEGER NOT NULL,
                    prediction_json TEXT,
                    latency_ms REAL NOT NULL,
                    usage_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _make_key(
        tier: str,
        model_role: str,
        model_id: str,
        prompt_version: Optional[str],
        input_sha256: str,
        pass_index: int,
    ) -> str:
        pv = prompt_version if prompt_version is not None else "__none__"
        return f"{tier}:{model_role}:{model_id}:{pv}:{input_sha256}:{pass_index}"

    def get(
        self,
        tier: str,
        model_role: str,
        model_id: str,
        prompt_version: Optional[str],
        input_sha256: str,
        pass_index: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Look up cached entry by composite key."""
        key = self._make_key(tier, model_role, model_id, prompt_version, input_sha256, pass_index)
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT prediction_json, latency_ms, usage_json, error
                FROM tier_cache
                WHERE cache_key = ?
                """,
                (key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            pred_json, latency_ms, usage_json, error = row
            pred_dict = json.loads(pred_json) if pred_json else None
            usage_dict = json.loads(usage_json) if usage_json else None
            return {
                "prediction": pred_dict,
                "latency_ms": float(latency_ms),
                "usage": usage_dict,
                "error": error,
            }

    def put(
        self,
        tier: str,
        model_role: str,
        model_id: str,
        prompt_version: Optional[str],
        input_sha256: str,
        entry: Dict[str, Any],
        pass_index: int = 0,
    ) -> None:
        """Store or update entry in tier cache."""
        key = self._make_key(tier, model_role, model_id, prompt_version, input_sha256, pass_index)
        pred_dict = entry.get("prediction")
        pred_json = json.dumps(pred_dict, sort_keys=True) if pred_dict is not None else None
        usage_dict = entry.get("usage")
        usage_json = json.dumps(usage_dict, sort_keys=True) if usage_dict is not None else None
        latency_ms = float(entry.get("latency_ms", 0.0))
        error = entry.get("error")

        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tier_cache (
                    cache_key, tier, model_role, model_id, prompt_version,
                    input_sha256, pass_index, prediction_json, latency_ms,
                    usage_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    tier,
                    model_role,
                    model_id,
                    prompt_version,
                    input_sha256,
                    pass_index,
                    pred_json,
                    latency_ms,
                    usage_json,
                    error,
                    time.time(),
                ),
            )
            conn.commit()

    def has(
        self,
        tier: str,
        model_role: str,
        model_id: str,
        prompt_version: Optional[str],
        input_sha256: str,
        pass_index: int = 0,
    ) -> bool:
        """Check if an entry exists in the cache."""
        return self.get(tier, model_role, model_id, prompt_version, input_sha256, pass_index) is not None

    def count(self) -> int:
        """Total number of cached entries."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM tier_cache")
            row = cursor.fetchone()
            return int(row[0]) if row else 0


class CachingS1Client:
    """Caching wrapper for Tier 1 router-s1 encoder client.

    # REQ-REVAL-011: Exposes route(text) -> RouterPrediction.
    """

    def __init__(
        self,
        inner: Optional[Any],
        cache: TierCache,
        mode: str = "replay",
        model_role: str = "router_s1",
        model_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        tier: str = "tier1_router_s1",
        refresh_cache: bool = False,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.mode = mode.lower()  # "live", "replay", "replay_or_live"
        self.model_role = model_role
        self.model_id = model_id or "router-s1"
        self.prompt_version = prompt_version
        self.tier = tier
        self.refresh_cache = refresh_cache
        self.calls: List[Dict[str, Any]] = []

    def route(self, text: str) -> RouterPrediction:
        """Execute router-s1 prediction with caching and call tracking."""
        input_sha = compute_input_sha256(text)
        cached = self.cache.get(
            tier=self.tier,
            model_role=self.model_role,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            input_sha256=input_sha,
            pass_index=0,
        )

        call_record: Dict[str, Any] = {
            "tier": self.tier,
            "model_role": self.model_role,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "pass_index": 0,
            "purpose": "decision",
            "cache_hit": False,
            "latency_ms": 0.0,
            "latency_measured": False,
            "usage": None,
            "cost_usd": 0.0,
            "error": None,
            "prediction": None,
        }

        def record_call(rec: Dict[str, Any]) -> None:
            self.calls.append(rec)
            cur_log = get_current_call_log()
            if cur_log is not None:
                cur_log.append(rec)

        if not self.refresh_cache:
            if self.mode == "replay":
                if cached is not None and cached.get("prediction") is not None:
                    call_record["cache_hit"] = True
                    call_record["latency_ms"] = float(cached.get("latency_ms", 0.0))
                    call_record["latency_measured"] = bool(cached.get("latency_measured", False))
                    call_record["usage"] = cached.get("usage")
                    call_record["prediction"] = cached["prediction"]
                    record_call(call_record)
                    return prediction_from_dict(cached["prediction"])

                key_tuple = (self.tier, self.model_role, self.model_id, self.prompt_version, input_sha, 0)
                call_record["error"] = f"Cache miss in replay mode: {key_tuple}"
                record_call(call_record)
                raise CacheMiss(key_tuple)

            # live or replay_or_live
            if self.mode == "replay_or_live" and cached is not None and cached.get("prediction") is not None:
                call_record["cache_hit"] = True
                call_record["latency_ms"] = float(cached.get("latency_ms", 0.0))
                call_record["latency_measured"] = bool(cached.get("latency_measured", False))
                call_record["usage"] = cached.get("usage")
                call_record["prediction"] = cached["prediction"]
                record_call(call_record)
                return prediction_from_dict(cached["prediction"])

        if self.inner is None:
            err = "Cannot execute live Tier 1 call: inner client is None"
            call_record["error"] = err
            record_call(call_record)
            raise RuntimeError(err)

        t0 = time.perf_counter()
        try:
            pred = self.inner.route(text)
            latency_ms = (time.perf_counter() - t0) * 1000.0

            pred_dict = prediction_to_dict(pred)
            usage = pred.raw.get("usage") if pred.raw else None

            call_record["cache_hit"] = False
            call_record["latency_ms"] = latency_ms
            call_record["latency_measured"] = True
            call_record["usage"] = usage
            call_record["prediction"] = pred_dict
            record_call(call_record)

            self.cache.put(
                tier=self.tier,
                model_role=self.model_role,
                model_id=self.model_id,
                prompt_version=self.prompt_version,
                input_sha256=input_sha,
                entry={
                    "prediction": pred_dict,
                    "latency_ms": latency_ms,
                    "latency_measured": True,
                    "usage": usage,
                    "error": None,
                },
                pass_index=0,
            )
            return pred
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            call_record["error"] = str(exc)
            call_record["latency_ms"] = latency_ms
            call_record["latency_measured"] = True
            record_call(call_record)
            raise


class CachingTier2Invoker:
    """Caching wrapper for Tier 2 Gemini Flash fallback invoker.

    # REQ-REVAL-011: Callable(text) -> RouterPrediction with pass_index support.
    """

    def __init__(
        self,
        inner: Optional[Callable[[str], RouterPrediction]],
        cache: TierCache,
        mode: str = "replay",
        pass_index: int = 0,
        model_role: str = "router_tier2",
        model_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        tier: str = "tier2_flash",
        refresh_cache: bool = False,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.mode = mode.lower()  # "live", "replay", "replay_or_live"
        self.pass_index = pass_index
        self.model_role = model_role
        if model_id is not None:
            self.model_id = model_id
        else:
            try:
                from ticket_router.config.models import model_id as get_model_id

                self.model_id = get_model_id(self.model_role)
            except Exception:
                self.model_id = ""
        self.prompt_version = prompt_version
        self.tier = tier
        self.refresh_cache = refresh_cache
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        text: str,
        pass_index: Optional[int] = None,
        purpose: Optional[str] = None,
    ) -> RouterPrediction:
        """Invoke Tier 2 fallback inference with caching and telemetry capture."""
        effective_pass = self.pass_index if pass_index is None else pass_index
        effective_purpose = purpose or ("reliability" if (effective_pass and effective_pass > 0) else "decision")
        input_sha = compute_input_sha256(text)

        cached = self.cache.get(
            tier=self.tier,
            model_role=self.model_role,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            input_sha256=input_sha,
            pass_index=effective_pass,
        )

        call_record: Dict[str, Any] = {
            "tier": self.tier,
            "model_role": self.model_role,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "pass_index": effective_pass,
            "purpose": effective_purpose,
            "cache_hit": False,
            "latency_ms": 0.0,
            "latency_measured": False,
            "usage": None,
            "cost_usd": 0.0,
            "error": None,
            "prediction": None,
        }

        def record_call(rec: Dict[str, Any]) -> None:
            self.calls.append(rec)
            cur_log = get_current_call_log()
            if cur_log is not None:
                cur_log.append(rec)

        if not self.refresh_cache:
            if self.mode == "replay":
                if cached is not None and cached.get("prediction") is not None:
                    call_record["cache_hit"] = True
                    call_record["latency_ms"] = float(cached.get("latency_ms", 0.0))
                    call_record["latency_measured"] = bool(cached.get("latency_measured", False))
                    call_record["usage"] = cached.get("usage")
                    call_record["prediction"] = cached["prediction"]
                    record_call(call_record)
                    return prediction_from_dict(cached["prediction"])

                key_tuple = (
                    self.tier,
                    self.model_role,
                    self.model_id,
                    self.prompt_version,
                    input_sha,
                    effective_pass,
                )
                call_record["error"] = f"Cache miss in replay mode: {key_tuple}"
                record_call(call_record)
                raise CacheMiss(key_tuple)

            # live or replay_or_live
            if self.mode == "replay_or_live" and cached is not None and cached.get("prediction") is not None:
                call_record["cache_hit"] = True
                call_record["latency_ms"] = float(cached.get("latency_ms", 0.0))
                call_record["latency_measured"] = bool(cached.get("latency_measured", False))
                call_record["usage"] = cached.get("usage")
                call_record["prediction"] = cached["prediction"]
                record_call(call_record)
                return prediction_from_dict(cached["prediction"])

        if self.inner is None:
            err = "Cannot execute live Tier 2 call: inner invoker is None"
            call_record["error"] = err
            record_call(call_record)
            raise RuntimeError(err)

        t0 = time.perf_counter()
        try:
            try:
                pred = self.inner(text, pass_index=effective_pass)
            except TypeError:
                pred = self.inner(text)
            latency_ms = (time.perf_counter() - t0) * 1000.0

            pred_dict = prediction_to_dict(pred)
            usage = pred.raw.get("usage") if pred.raw else None
            resolved_model_id = (pred.raw.get("model_id") if pred.raw else None) or self.model_id
            call_record["model_id"] = resolved_model_id
            call_record["cache_hit"] = False
            call_record["latency_ms"] = latency_ms
            call_record["latency_measured"] = True
            call_record["usage"] = usage
            call_record["prediction"] = pred_dict
            record_call(call_record)

            self.cache.put(
                tier=self.tier,
                model_role=self.model_role,
                model_id=resolved_model_id,
                prompt_version=self.prompt_version,
                input_sha256=input_sha,
                entry={
                    "prediction": pred_dict,
                    "latency_ms": latency_ms,
                    "latency_measured": True,
                    "usage": usage,
                    "error": None,
                },
                pass_index=effective_pass,
            )
            return pred
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            call_record["error"] = str(exc)
            call_record["latency_ms"] = latency_ms
            call_record["latency_measured"] = True
            record_call(call_record)
            raise
