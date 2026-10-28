"""Evaluation runner for routing cascade policies, replays, and deployed targets.

# REQ-REVAL-011: Runner targets, tier cache, dry-run & cost cap
# REQ-REVAL-015: Stochastic-tier reliability data collection (k-pass)
# REQ-REVAL-018: Cost & latency integration
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

from evals.routing.cache import (
    CacheMiss,
    CachingS1Client,
    CachingTier2Invoker,
    TierCache,
    compute_input_sha256,
    set_current_call_log,
)
from evals.routing.cost import call_cost, estimate_dry_run_cost
from evals.routing.trace import (
    TraceRecord,
)
from ticket_router.agents.triage_router.policy import (
    RouterDecision,
    RouterThresholds,
    TriagePolicy,
    evaluate_tier0_rules,
)
from ticket_router.config.agent_tuning import (
    _CONTEXT_TIER2_VERSION,
    PROMPTS_DIR,
    AgentTuningConfig,
    get_tier2_prompt,
    get_tuning_config,
)
from ticket_router.config.models import model_id
from ticket_router.models.router_s1_client import RouterPrediction

logger = logging.getLogger("evals.routing.runner")

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATES_DIR = REPO_ROOT / "evals" / "candidates"  # or evals/routing/candidates
DEFAULT_CANDIDATES_DIR = REPO_ROOT / "evals" / "routing" / "candidates"
DEFAULT_RESULTS_DIR = REPO_ROOT / "evals" / "results" / "routing"
DEFAULT_CACHE_DIR = REPO_ROOT / ".cache" / "routing_eval" / "tier_cache"
DEFAULT_RUNS_CACHE_DIR = REPO_ROOT / ".cache" / "routing_eval" / "runs"
DEFAULT_CONFIG_PATH = REPO_ROOT / "evals" / "routing" / "config.yaml"


class CostCapExceeded(Exception):
    """Raised when estimated or actual live execution cost exceeds the configured cost cap."""



def load_eval_config(config_path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Load evals/routing/config.yaml."""
    target = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if target.is_file():
        with target.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def next_run_dir(
    root: Path | str = DEFAULT_RESULTS_DIR,
    label: Optional[str] = None,
) -> Path:
    """Find max existing run_NNN directory in root and create run_{NNN+1}_{label}.

    Guarantees run directories are never overwritten.
    """
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)

    max_idx = 0
    pattern = re.compile(r"^run_(\d{3})(?:_.*)?$")
    for child in root_path.iterdir():
        if child.is_dir():
            m = pattern.match(child.name)
            if m:
                idx = int(m.group(1))
                if idx > max_idx:
                    max_idx = idx

    next_idx = max_idx + 1
    dir_name = f"run_{next_idx:03d}_{label}" if label else f"run_{next_idx:03d}"
    target_dir = root_path / dir_name
    target_dir.mkdir(parents=True, exist_ok=False)
    return target_dir


def load_candidate_overlay(candidate: Optional[Dict[str, Any] | str | Path]) -> Tuple[str, Dict[str, Any]]:
    """Resolve and load candidate configuration overlay."""
    if candidate is None or candidate == "" or candidate == "baseline":
        baseline_file = DEFAULT_CANDIDATES_DIR / "baseline.yaml"
        if baseline_file.is_file():
            with baseline_file.open("r", encoding="utf-8") as f:
                return "baseline", yaml.safe_load(f) or {}
        return "baseline", {}

    if isinstance(candidate, dict):
        return "custom_overlay", candidate

    cand_str = str(candidate)
    cand_path = Path(cand_str)
    if cand_path.is_file():
        with cand_path.open("r", encoding="utf-8") as f:
            return cand_path.stem, yaml.safe_load(f) or {}

    # Check candidates directory
    for search_dir in (DEFAULT_CANDIDATES_DIR, CANDIDATES_DIR):
        f1 = search_dir / f"{cand_str}.yaml"
        f2 = search_dir / f"{cand_str}.yml"
        if f1.is_file():
            with f1.open("r", encoding="utf-8") as f:
                return cand_str, yaml.safe_load(f) or {}
        if f2.is_file():
            with f2.open("r", encoding="utf-8") as f:
                return cand_str, yaml.safe_load(f) or {}

    raise FileNotFoundError(f"Candidate overlay not found: '{cand_str}' in {DEFAULT_CANDIDATES_DIR}")


def compute_config_hash(
    merged_tuning: AgentTuningConfig,
    model_ids: Dict[str, str],
    prompt_versions: Dict[str, str],
) -> str:
    """Compute deterministic SHA-256 hash of canonical configuration tuple."""
    canonical_dict = {
        "agent_tuning": {
            "tau_esc": float(merged_tuning.tau_esc),
            "tau_low": float(merged_tuning.tau_low),
            "tau_intent": float(merged_tuning.tau_intent),
            "tier2_router_version": str(merged_tuning.tier2_router_version),
            "faq_answer_version": str(merged_tuning.faq_answer_version),
            "search_config": str(merged_tuning.search_config),
            "top_k": int(merged_tuning.top_k),
            "rerank_enabled": bool(merged_tuning.rerank_enabled),
            "citation_strictness": float(merged_tuning.citation_strictness),
        },
        "model_ids": dict(sorted(model_ids.items())),
        "prompt_versions": dict(sorted(prompt_versions.items())),
    }
    canon_bytes = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon_bytes).hexdigest()


def get_git_info() -> Tuple[str, bool]:
    """Retrieve current Git commit SHA and dirty working tree status."""
    try:
        res_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        sha = res_sha.stdout.strip()
        res_diff = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True)
        dirty = bool(res_diff.stdout.strip())
        return sha, dirty
    except Exception:
        return "unknown", False


def _case_text(case: Any) -> str:
    """Extract full ticket text from case object or dictionary."""
    if hasattr(case, "full_text"):
        return case.full_text
    if hasattr(case, "subject") and hasattr(case, "description"):
        subj = getattr(case, "subject", "") or ""
        desc = getattr(case, "description", "") or ""
        return f"{subj}\n\n{desc}" if subj else desc
    if isinstance(case, dict):
        subj = case.get("subject") or ""
        desc = case.get("description") or case.get("message") or ""
        return f"{subj}\n\n{desc}" if subj else desc
    return str(case)


def _case_ticket_id(case: Any) -> str:
    """Extract ticket ID from case object or dictionary."""
    if hasattr(case, "ticket_id"):
        return str(case.ticket_id)
    if isinstance(case, dict):
        return str(case.get("ticket_id") or case.get("case_id") or "unknown_ticket")
    return "unknown_ticket"


def _case_split(case: Any) -> str:
    """Extract split label from case object or dictionary."""
    if hasattr(case, "split"):
        return str(case.split)
    if isinstance(case, dict):
        return str(case.get("split", "dev"))
    return "dev"


def _make_live_tier2_invoker(target_model: str, p_ver: Optional[str] = None) -> Callable[[str], RouterPrediction]:
    """Build live Tier 2 Gemini Flash callable setting context-local prompt version."""
    from ticket_router.agents.triage_router.agent import invoke_tier2_gemini

    def _invoker(text: str) -> RouterPrediction:
        token = _CONTEXT_TIER2_VERSION.set(p_ver)
        try:
            return invoke_tier2_gemini(text, target_model=target_model)
        finally:
            _CONTEXT_TIER2_VERSION.reset(token)

    return _invoker


def run(
    cases: Sequence[Any],
    target: str = "policy",
    candidate_overlay: Optional[Dict[str, Any] | str | Path] = None,
    run_dir: Optional[Path | str] = None,
    *,
    mode: Optional[str] = None,
    max_cost_usd: float = 5.0,
    concurrency: int = 8,
    reliability_k: int = 1,
    with_model_armor: bool = False,
    dry_run: bool = False,
    label: Optional[str] = None,
    cache_root: Optional[Path | str] = None,
    config_path: Optional[Path | str] = None,
    manifest_sha256: Optional[str] = None,
    s1_client_override: Optional[Any] = None,
    tier2_invoker_override: Optional[Any] = None,
    model_armor_override: Optional[Any] = None,
) -> List[TraceRecord]:
    """Execute evaluation run over ticket cases with deterministic caching and telemetry.

    # REQ-REVAL-011: Runner targets (policy, replay, deployed), tier cache, dry-run, cost cap.
    # REQ-REVAL-015: k-pass Tier 2 stochastic reliability recording.
    # REQ-REVAL-018: Cost and latency telemetry.
    """
    eval_cfg = load_eval_config(config_path)
    cand_name, overlay_dict = load_candidate_overlay(candidate_overlay)
    merged_tuning = get_tuning_config(reload=True, overlay=overlay_dict)

    # Validate prompt version
    prompt_version = merged_tuning.tier2_router_version
    template_path = PROMPTS_DIR / f"tier2_router_{prompt_version}.yaml"
    if prompt_version != "v1" and not template_path.is_file():
        raise FileNotFoundError(
            f"Tier 2 prompt version '{prompt_version}' referenced by candidate overlay '{cand_name}' "
            f"not found at '{template_path}'"
        )

    # Resolve models
    t2_model_id = model_id("router_tier2")
    s1_model_id = "router-s1"
    model_ids = {
        "router_tier2": t2_model_id,
        "router_s1": s1_model_id,
    }
    prompt_versions = {
        "tier2_router_version": prompt_version,
        "faq_answer_version": merged_tuning.faq_answer_version,
    }
    config_hash = compute_config_hash(merged_tuning, model_ids, prompt_versions)

    # Establish run directory
    resolved_run_dir = Path(run_dir) if run_dir else next_run_dir(label=label or cand_name)
    resolved_run_dir.mkdir(parents=True, exist_ok=True)
    run_id = resolved_run_dir.name

    # Determine execution mode
    eff_target = target.lower()
    if mode is not None:
        eff_mode = mode.lower()
    elif eff_target == "replay":
        eff_mode = "replay"
    else:
        eff_mode = "live"

    # Initialize tier cache
    cache = TierCache(root=cache_root or DEFAULT_CACHE_DIR)

    # -------------------------------------------------------------------------
    # Dry Run Mode (REQ-REVAL-011)
    # -------------------------------------------------------------------------
    if dry_run:
        sample_prompts: List[Dict[str, Any]] = []
        all_prompt_hashes: List[str] = []
        t1_hits = 0
        t1_misses = 0
        t2_hits = 0
        t2_misses = 0

        for case in cases:
            text = _case_text(case)
            t_id = _case_ticket_id(case)
            input_sha = compute_input_sha256(text)

            p_text = get_tier2_prompt(text, version=prompt_version)
            p_sha = compute_input_sha256(p_text)
            all_prompt_hashes.append(p_sha)

            if len(sample_prompts) < 5:
                sample_prompts.append({
                    "ticket_id": t_id,
                    "prompt": p_text,
                    "sha256": p_sha,
                })

            if cache.has(
                tier="tier1_router_s1",
                model_role="router_s1",
                model_id=s1_model_id,
                prompt_version=None,
                input_sha256=input_sha,
                pass_index=0,
            ):
                t1_hits += 1
            else:
                t1_misses += 1

            if cache.has(
                tier="tier2_flash",
                model_role="router_tier2",
                model_id=t2_model_id,
                prompt_version=prompt_version,
                input_sha256=input_sha,
                pass_index=0,
            ):
                t2_hits += 1
            else:
                t2_misses += 1

        misses_stats = {
            "tier1_router_s1": t1_misses,
            "tier2_flash": t2_misses,
        }
        est_cost = estimate_dry_run_cost(misses_stats, eval_cfg)

        dry_run_payload = {
            "is_dry_run": True,
            "run_id": run_id,
            "target": eff_target,
            "candidate": cand_name,
            "cases_count": len(cases),
            "cache_stats": {
                "tier1_router_s1": {"hits": t1_hits, "misses": t1_misses},
                "tier2_flash": {"hits": t2_hits, "misses": t2_misses},
            },
            "estimated_cost_usd": round(est_cost, 6),
            "sample_tier2_prompts": sample_prompts,
            "all_tier2_prompt_hashes": all_prompt_hashes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with (resolved_run_dir / "dry_run.json").open("w", encoding="utf-8") as f:
            json.dump(dry_run_payload, f, indent=2)

        # Build dry-run traces without live calls
        dry_records: List[TraceRecord] = []
        for case in cases:
            t_id = _case_ticket_id(case)
            split_name = _case_split(case)
            pred_stub = {
                "intent": "other",
                "decision": "escalate",
                "queue": "human-standard",
                "escalate": True,
                "sentiment": "neutral",
                "urgency": "normal",
                "tier": "dry_run",
                "degraded": False,
                "reason": "dry_run_simulation",
            }
            dry_records.append(
                TraceRecord(
                    ticket_id=t_id,
                    split=split_name,
                    target=eff_target,
                    run_id=run_id,
                    config_hash=config_hash,
                    candidate=cand_name,
                    predicted=pred_stub,
                    decision_raw={},
                    tier_calls=[],
                    reliability=None,
                    latency_ms_total=0.0,
                    cost_usd_total=0.0,
                    error=None,
                    is_dry_run=True,
                )
            )

        dry_records.sort(key=lambda r: r.ticket_id)
        _save_run_artifacts(resolved_run_dir, run_id, dry_records, config_hash, cand_name, eff_target, merged_tuning, model_ids, prompt_versions, manifest_sha256, {
            "max_cost_usd": max_cost_usd,
            "concurrency": concurrency,
            "reliability_k": reliability_k,
            "with_model_armor": with_model_armor,
            "dry_run": True,
            "mode": eff_mode,
        })
        return dry_records

    # -------------------------------------------------------------------------
    # Pre-Run Cost Cap Check (REQ-REVAL-011)
    # -------------------------------------------------------------------------
    if eff_mode != "replay":
        # Check cache misses for pre-run estimate
        misses_count: Dict[str, int] = {"tier1_router_s1": 0, "tier2_flash": 0}
        for case in cases:
            text = _case_text(case)
            input_sha = compute_input_sha256(text)
            if not cache.has("tier1_router_s1", "router_s1", s1_model_id, None, input_sha, 0):
                misses_count["tier1_router_s1"] += 1
            if not cache.has("tier2_flash", "router_tier2", t2_model_id, prompt_version, input_sha, 0):
                misses_count["tier2_flash"] += 1

        est_cost = estimate_dry_run_cost(misses_count, eval_cfg)
        if est_cost > max_cost_usd:
            raise CostCapExceeded(
                f"Estimated pre-run live cost ${est_cost:.4f} exceeds cost cap ${max_cost_usd:.4f}. "
                "Aborting before initiating any live network requests."
            )

    # -------------------------------------------------------------------------
    # Setup Policy and Wrappers
    # -------------------------------------------------------------------------
    thresholds = RouterThresholds(
        tau_esc=float(merged_tuning.tau_esc),
        tau_low=float(merged_tuning.tau_low),
        tau_intent=float(merged_tuning.tau_intent),
    )

    live_s1_inner = s1_client_override
    live_t2_inner = tier2_invoker_override
    live_ma_inner = model_armor_override

    if eff_mode != "replay":
        if live_s1_inner is None and eff_target == "policy":
            try:
                from ticket_router.models.router_s1_client import RouterS1Client

                live_s1_inner = RouterS1Client()
            except Exception as exc:
                logger.warning("Could not construct live RouterS1Client: %s", exc)

        if live_t2_inner is None and eff_target == "policy":
            try:
                live_t2_inner = _make_live_tier2_invoker(t2_model_id, prompt_version)
            except Exception as exc:
                logger.warning("Could not construct live Tier 2 invoker: %s", exc)

        if with_model_armor and live_ma_inner is None:
            try:
                from ticket_router.security.model_armor import ModelArmorClient

                live_ma_inner = ModelArmorClient()
            except Exception as exc:
                logger.warning("Could not construct live ModelArmorClient: %s", exc)

    caching_s1 = CachingS1Client(
        inner=live_s1_inner,
        cache=cache,
        mode=eff_mode,
        model_role="router_s1",
        model_id=s1_model_id,
        prompt_version=None,
    )
    caching_t2 = CachingTier2Invoker(
        inner=live_t2_inner,
        cache=cache,
        mode=eff_mode,
        pass_index=0,
        model_role="router_tier2",
        model_id=t2_model_id,
        prompt_version=prompt_version,
    )

    policy = TriagePolicy(
        thresholds=thresholds,
        s1_client=caching_s1,
        tier2_invoker=caching_t2,
        model_armor_client=live_ma_inner if with_model_armor else None,
    )

    # Thread-safe cost cap tracker
    spent_lock = threading.Lock()
    cumulative_live_cost = 0.0
    cost_cap_aborted = False

    def check_and_record_cost(call_rec: Dict[str, Any]) -> None:
        nonlocal cumulative_live_cost, cost_cap_aborted
        c_usd = call_cost(call_rec, eval_cfg)
        call_rec["cost_usd"] = c_usd
        if not call_rec.get("cache_hit"):
            with spent_lock:
                cumulative_live_cost += c_usd
                if cumulative_live_cost > max_cost_usd:
                    cost_cap_aborted = True

    # -------------------------------------------------------------------------
    # Single Case Evaluation Worker
    # -------------------------------------------------------------------------
    def evaluate_case(case: Any) -> TraceRecord:
        nonlocal cost_cap_aborted
        t_id = _case_ticket_id(case)
        split_name = _case_split(case)
        text = _case_text(case)

        token_ver = None
        if prompt_version:
            token_ver = _CONTEXT_TIER2_VERSION.set(prompt_version)

        try:
            return _evaluate_case_inner(case, t_id, split_name, text)
        finally:
            if token_ver is not None:
                _CONTEXT_TIER2_VERSION.reset(token_ver)

    def _evaluate_case_inner(case: Any, t_id: str, split_name: str, text: str) -> TraceRecord:
        nonlocal cost_cap_aborted

        if cost_cap_aborted:
            pred_stub = {
                "intent": "other",
                "decision": "escalate",
                "queue": "human-standard",
                "escalate": True,
                "sentiment": "neutral",
                "urgency": "normal",
                "tier": "abort",
                "degraded": True,
                "reason": "cost_cap_abort",
            }
            return TraceRecord(
                ticket_id=t_id,
                split=split_name,
                target=eff_target,
                run_id=run_id,
                config_hash=config_hash,
                candidate=cand_name,
                predicted=pred_stub,
                decision_raw={},
                tier_calls=[],
                reliability=None,
                latency_ms_total=0.0,
                cost_usd_total=0.0,
                error="cost_cap_abort",
                is_dry_run=False,
            )

        case_calls: List[Dict[str, Any]] = []
        set_current_call_log(case_calls)

        t0_wall = time.monotonic()
        decision: Optional[RouterDecision] = None
        error_msg: Optional[str] = None

        if eff_target == "deployed":
            # Deployed Agent Engine target
            try:
                from evals.agent_e2e import DeployedAgentRunner

                runner = DeployedAgentRunner(dry_run=False)
                res = runner.run_case({"message": text, "case_id": t_id})
                t_total = (time.monotonic() - t0_wall) * 1000.0

                dec_str = str(res.get("decision", "escalate"))
                is_esc = bool(res.get("escalate", dec_str == "escalate"))
                predicted = {
                    "intent": str(res.get("predicted_intent", "other")),
                    "decision": dec_str,
                    "queue": str(res.get("queue", "human-standard" if is_esc else "none")),
                    "escalate": is_esc,
                    "sentiment": "neutral",
                    "urgency": "normal",
                    "tier": "deployed",
                    "degraded": False,
                    "reason": "deployed_agent_engine",
                }
                return TraceRecord(
                    ticket_id=t_id,
                    split=split_name,
                    target="deployed",
                    run_id=run_id,
                    config_hash=config_hash,
                    candidate=cand_name,
                    predicted=predicted,
                    decision_raw=res,
                    tier_calls=[],
                    reliability=None,
                    latency_ms_total=t_total,
                    cost_usd_total=float(res.get("cost_usd", 0.0)),
                    error=None,
                    is_dry_run=False,
                )
            except Exception as exc:
                t_total = (time.monotonic() - t0_wall) * 1000.0
                return TraceRecord(
                    ticket_id=t_id,
                    split=split_name,
                    target="deployed",
                    run_id=run_id,
                    config_hash=config_hash,
                    candidate=cand_name,
                    predicted={
                        "intent": "other",
                        "decision": "escalate",
                        "queue": "human-standard",
                        "escalate": True,
                        "sentiment": "neutral",
                        "urgency": "normal",
                        "tier": "deployed_error",
                        "degraded": True,
                        "reason": str(exc),
                    },
                    decision_raw={},
                    tier_calls=[],
                    reliability=None,
                    latency_ms_total=t_total,
                    cost_usd_total=0.0,
                    error=str(exc),
                    is_dry_run=False,
                )

        # Policy / Replay target with retries on transient errors
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                decision = policy.evaluate(
                    text=text,
                    session_id=f"eval-{t_id[:12]}",
                    customer_id="anon:eval",
                    verified_facts=None,
                    ticket_id=t_id,
                )
                break
            except CacheMiss:
                # CacheMiss is deterministically non-transient, re-raise immediately
                raise
            except Exception as exc:
                err_text = str(exc).lower()
                is_transient = any(code in err_text for code in ("429", "500", "503", "quota", "rate limit", "exhausted"))
                if is_transient and attempt < max_attempts - 1:
                    time.sleep(0.05 * (2**attempt))
                    continue
                error_msg = str(exc)
                break

        t_total_ms = (time.monotonic() - t0_wall) * 1000.0

        # Calculate costs for calls made in this case
        for call_rec in case_calls:
            check_and_record_cost(call_rec)

        # Handle stochastic reliability passes if Tier 2 decided
        rel_record: Optional[Dict[str, Any]] = None
        if decision is not None and decision.tier == "tier2_flash":
            passes: List[Dict[str, Any]] = [
                {
                    "intent": decision.predicted_intent,
                    "escalate": decision.escalate,
                    "decision": decision.decision,
                }
            ]

            if reliability_k > 1:
                # Cache key uses pass_index 1..k-1
                tier0 = evaluate_tier0_rules(text)
                for p_idx in range(1, reliability_k):
                    try:
                        p_pred = caching_t2(text, pass_index=p_idx)
                        final_esc = (
                            tier0.escalate
                            or p_pred.escalate
                            or p_pred.sentiment == "angry"
                            or p_pred.urgency == "immediate"
                        )
                        dec_str = "escalate" if final_esc else "auto_rag"
                        passes.append({
                            "intent": p_pred.intent,
                            "escalate": final_esc,
                            "decision": dec_str,
                        })
                    except Exception as p_exc:
                        passes.append({
                            "intent": "other",
                            "escalate": True,
                            "decision": "escalate",
                            "error": str(p_exc),
                        })

                # Record costs for the extra passes
                for call_rec in case_calls[1:]:
                    check_and_record_cost(call_rec)

            rel_record = {"passes": passes}

        # Build final predicted summary
        if decision is not None:
            predicted = {
                "intent": decision.predicted_intent,
                "decision": decision.decision,
                "queue": decision.queue,
                "escalate": decision.escalate,
                "sentiment": decision.sentiment,
                "urgency": decision.urgency,
                "tier": decision.tier,
                "degraded": decision.degraded,
                "reason": decision.reason,
            }
            dec_raw = {
                "ticket_id": decision.ticket_id,
                "session_id": decision.session_id,
                "decision": decision.decision,
                "tier": decision.tier,
                "queue": decision.queue,
                "predicted_intent": decision.predicted_intent,
                "escalate": decision.escalate,
                "sentiment": decision.sentiment,
                "urgency": decision.urgency,
                "reason": decision.reason,
                "degraded": decision.degraded,
                "latency_ms": decision.latency_ms,
                "details": decision.details,
            }
        else:
            predicted = {
                "intent": "other",
                "decision": "escalate",
                "queue": "human-standard",
                "escalate": True,
                "sentiment": "neutral",
                "urgency": "normal",
                "tier": "policy_error",
                "degraded": True,
                "reason": error_msg or "evaluation_failed",
            }
            dec_raw = {}

        total_case_cost = sum(c.get("cost_usd", 0.0) for c in case_calls)

        return TraceRecord(
            ticket_id=t_id,
            split=split_name,
            target=eff_target,
            run_id=run_id,
            config_hash=config_hash,
            candidate=cand_name,
            predicted=predicted,
            decision_raw=dec_raw,
            tier_calls=case_calls,
            reliability=rel_record,
            latency_ms_total=t_total_ms,
            cost_usd_total=total_case_cost,
            error=error_msg,
            is_dry_run=False,
        )

    # -------------------------------------------------------------------------
    # Execute Cases Concurrently with Bounded ThreadPool
    # -------------------------------------------------------------------------
    records: List[TraceRecord] = []
    eff_workers = max(1, concurrency)

    if eff_workers == 1:
        for case in cases:
            records.append(evaluate_case(case))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=eff_workers) as executor:
            future_to_case = {executor.submit(evaluate_case, c): c for c in cases}
            try:
                for fut in concurrent.futures.as_completed(future_to_case):
                    try:
                        records.append(fut.result())
                    except CacheMiss:
                        # In replay mode, CacheMiss should bubble up unless caught
                        raise
            except BaseException:
                executor.shutdown(wait=False, cancel_futures=True)
                raise

    # Deterministic output order: sorted strictly by ticket_id
    records.sort(key=lambda r: r.ticket_id)

    # Save artifacts
    _save_run_artifacts(
        resolved_run_dir,
        run_id,
        records,
        config_hash,
        cand_name,
        eff_target,
        merged_tuning,
        model_ids,
        prompt_versions,
        manifest_sha256,
        {
            "max_cost_usd": max_cost_usd,
            "concurrency": concurrency,
            "reliability_k": reliability_k,
            "with_model_armor": with_model_armor,
            "dry_run": False,
            "mode": eff_mode,
        },
    )

    return records


def _save_run_artifacts(
    run_dir: Path,
    run_id: str,
    records: List[TraceRecord],
    config_hash: str,
    candidate: str,
    target: str,
    tuning: AgentTuningConfig,
    model_ids: Dict[str, str],
    prompt_versions: Dict[str, str],
    manifest_sha256: Optional[str],
    args_dict: Dict[str, Any],
) -> None:
    """Save config_snapshot.json and traces.jsonl to run_dir and centralized cache."""
    git_sha, git_dirty = get_git_info()

    snapshot = {
        "run_id": run_id,
        "config_hash": config_hash,
        "target": target,
        "candidate": candidate,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "merged_agent_tuning": {
            "tau_esc": tuning.tau_esc,
            "tau_low": tuning.tau_low,
            "tau_intent": tuning.tau_intent,
            "tier2_router_version": tuning.tier2_router_version,
            "faq_answer_version": tuning.faq_answer_version,
            "search_config": tuning.search_config,
            "top_k": tuning.top_k,
            "rerank_enabled": tuning.rerank_enabled,
            "citation_strictness": tuning.citation_strictness,
        },
        "model_ids": model_ids,
        "prompt_versions": prompt_versions,
        "dataset_manifest_sha256": manifest_sha256,
        "args": args_dict,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    with (run_dir / "config_snapshot.json").open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    # Save traces.jsonl in run_dir
    traces_path = run_dir / "traces.jsonl"
    with traces_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")

    # Also save to centralized run cache directory
    cache_run_dir = DEFAULT_RUNS_CACHE_DIR / run_id
    cache_run_dir.mkdir(parents=True, exist_ok=True)
    with (cache_run_dir / "traces.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
