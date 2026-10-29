"""Pipeline driver and reproducible recipe runner for routing_v1.

# REQ-REVAL-004 / REQ-REVAL-005 / REQ-REVAL-006 / REQ-REVAL-009 / REQ-REVAL-020:
Provides end-to-end data production pipeline stages:
- stage1: Collects real, public, deterministic template, perturbation, and regression sources into pool_stage1.jsonl;
          exports self-contained generation_packets.jsonl (~260 tickets in 10-ticket batches) and paraphrase_packets.jsonl (40 bases x 3);
          prints coverage matrix and top gaps.
- stage2: Ingests generated and paraphrase responses (code-fence tolerant, malformed-line safe);
          writes pool_stage2.jsonl;
          exports annotation_packets_pass{0,1,2}.jsonl (batched 20 tickets per packet, seeded label-order shuffle per pass).
- stage3: Ingests k=3 annotation passes; computes reliability report (alpha per field);
          derives consensus (never overwriting verified fields);
          exports adjudication_queue.tsv (18 REQ-REVAL-007 columns) + summary counts by queue reason;
          writes pool_stage3.jsonl.
- build: Ingests optional adjudication decisions; applies contamination guard (train_val + test excluded from heldout);
         exact-deduplicates, clusters near-duplicates, assigns group-aware splits, verifies zero leakage,
         computes coverage, builds manifest, and writes immutable version dir.
"""

from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import hashlib
import json
import pathlib
import random
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from evals.routing import (
    adjudicate,
    annotate,
    dedup,
    perturb,
    regression,
)
from evals.routing import (
    build as build_module,
)
from evals.routing.lang import is_english
from evals.routing.recipes import load_recipe
from evals.routing.schema import (
    Annotation,
    Expected,
    FailureRef,
    RoutingCase,
    Style,
    compute_content_hash,
    make_ticket_id,
    read_jsonl,
    validate_case,
    write_jsonl,
)
from evals.routing.sources import public_bitext, real_twcs
from ticket_router.config.taxonomy import load_taxonomy, validate_expected

_MODULE_DIR = pathlib.Path(__file__).resolve().parent
_GUIDELINES_PATH = _MODULE_DIR / "GUIDELINES.md"


def _compute_length_bucket(text: str) -> str:
    count = len(text.split())
    if count < 20:
        return "short (<20 words)"
    if count <= 80:
        return "medium (20–80)"
    return "long (>80)"


def _strip_code_fences(content: str) -> str:
    """Strip markdown code block fences (e.g. ```json ... ```) from content."""
    s = content.strip()
    # Match ```json ... ``` or ``` ... ```
    if s.startswith("```"):
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1 :]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _parse_json_lines_or_block(raw_text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse JSON objects from JSON-per-line, code-fenced blocks, or multiline JSON objects.

    Returns:
        (valid_objects, malformed_entries)
    """
    s = raw_text.strip()
    if not s:
        return [], []

    decoder = json.JSONDecoder()
    pos = 0
    valid: List[Dict[str, Any]] = []
    malformed: List[Dict[str, Any]] = []

    while pos < len(s):
        # Skip whitespace and markdown code block fences
        while pos < len(s) and s[pos] in " \t\r\n`":
            pos += 1
        if pos >= len(s):
            break

        # Check for JSON object
        if s[pos] == "{":
            try:
                obj, end = decoder.raw_decode(s, pos)
                if isinstance(obj, dict):
                    valid.append(obj)
                pos = end
            except json.JSONDecodeError as e:
                malformed.append({"pos": pos, "error": str(e), "snippet": s[pos : pos + 80]})
                next_newline = s.find("\n", pos)
                pos = len(s) if next_newline == -1 else next_newline + 1
        elif s[pos] == "[":
            try:
                arr, end = decoder.raw_decode(s, pos)
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, dict):
                            valid.append(item)
                pos = end
            except json.JSONDecodeError as e:
                malformed.append({"pos": pos, "error": str(e), "snippet": s[pos : pos + 80]})
                next_newline = s.find("\n", pos)
                pos = len(s) if next_newline == -1 else next_newline + 1
        else:
            # Advance past unparseable line or word
            next_newline = s.find("\n", pos)
            if next_newline == -1:
                break
            pos = next_newline + 1

    return valid, malformed


def validate_pool_cases(cases: Sequence[RoutingCase | Dict[str, Any]], stage_name: str) -> None:
    """Validate all cases against schema.validate_case and hard-fail if any row fails."""
    errors_by_type: Dict[str, int] = {}
    failing_cases: List[Tuple[str, List[str]]] = []

    for c in cases:
        raw = c.to_dict() if hasattr(c, "to_dict") else c
        t_id = raw.get("ticket_id", "unknown")
        errs = validate_case(raw)
        if errs:
            failing_cases.append((t_id, errs))
            for e in errs:
                errors_by_type[e] = errors_by_type.get(e, 0) + 1

    total = len(cases)
    failed = len(failing_cases)
    passed = total - failed

    print(f"\n[{stage_name.upper()} VALIDATION SUMMARY]")
    print(f"  Total cases:  {total}")
    print(f"  Passed cases: {passed}")
    print(f"  Failed cases: {failed}")

    if errors_by_type:
        print("  Validation error breakdown:")
        for err_msg, count in sorted(errors_by_type.items(), key=lambda x: -x[1]):
            print(f"    - [{count}] {err_msg}")
        sample_failures = failing_cases[:5]
        print("  Sample failing cases:")
        for t_id, err_list in sample_failures:
            print(f"    * {t_id}: {'; '.join(err_list)}")

    if failed > 0:
        raise ValueError(
            f"{stage_name} schema validation failed: {failed}/{total} cases failed schema.validate_case. "
            f"Fix schema violations before proceeding."
        )


def _ensure_case_schema_compliance(c: RoutingCase) -> RoutingCase:
    """Ensure case adheres to schema rules (e.g. non-English requires ood scenario and pool split)."""
    scens = list(c.scenarios or ())
    notes = c.notes or ""
    split = c.split
    lang = c.language

    # Rule: Non-English language is only allowed in 'pool' split
    if lang != "en":
        if split != "pool":
            split = "pool"
        if "ood" not in scens and "out_of_scope_language" not in notes:
            scens.append("ood")
            if not notes:
                notes = f"out_of_scope_language:{lang}"
            elif "out_of_scope_language" not in notes:
                notes = f"{notes}; out_of_scope_language:{lang}"

    updates = {}
    if tuple(scens) != c.scenarios:
        updates["scenarios"] = tuple(scens)
    if split != c.split:
        updates["split"] = split
    if notes != (c.notes or ""):
        updates["notes"] = notes

    if updates:
        return dataclasses.replace(c, **updates)
    return c


# ---------------------------------------------------------------------------
# STAGE 1: Collect candidates and export generation/paraphrase packets
# ---------------------------------------------------------------------------


def stage1(
    work_dir: pathlib.Path | str,
    recipe_path: Optional[pathlib.Path | str] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute pipeline stage 1: collect sources and emit offline synthesis packets.

    # REQ-REVAL-004 / REQ-REVAL-005: Source adapters & perturbation suites.
    """
    work = pathlib.Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    recipe = load_recipe(recipe_path=recipe_path)
    effective_seed = seed if seed is not None else int(recipe.get("seed", 20261028))
    version = str(recipe.get("dataset_version", "routing_v1.0.0"))
    tax = load_taxonomy()

    guidelines_excerpt = ""
    if _GUIDELINES_PATH.exists():
        guidelines_excerpt = _GUIDELINES_PATH.read_text(encoding="utf-8")[:2500]

    all_stage1_cases: List[RoutingCase] = []

    # 1. Real: Retrieval C2 human-adjudicated intent cases
    c2_cfg = recipe.get("sources", {}).get("real_c2", {})
    c2_cases: List[RoutingCase] = []
    if c2_cfg:
        c2_cases = real_twcs.load_retrieval_c2(
            labels_path=c2_cfg["labels_path"],
            queries_path=c2_cfg["queries_path"],
            manifest_path=c2_cfg["manifest_path"],
            dataset_version=version,
        )
        all_stage1_cases.extend(c2_cases)

    # 2. Real: Router Golden consensus
    golden_cfg = recipe.get("sources", {}).get("real_golden", {})
    golden_cases: List[RoutingCase] = []
    if golden_cfg:
        golden_cases = real_twcs.load_golden_consensus(
            golden_csv_path=golden_cfg["golden_csv_path"],
            dataset_version=version,
        )
        all_stage1_cases.extend(golden_cases)

    # 3. Public: Bitext stratified sample
    bitext_cfg = recipe.get("sources", {}).get("public_bitext", {})
    bitext_cases: List[RoutingCase] = []
    if bitext_cfg:
        bitext_cases = public_bitext.sample(
            csv_path=bitext_cfg["csv_path"],
            n=int(bitext_cfg.get("n", 300)),
            seed=effective_seed,
            per_intent_min=int(bitext_cfg.get("per_intent_min", 8)),
            dataset_version=version,
        )
        all_stage1_cases.extend(bitext_cases)

    # 4. Synthetic templates from recipe
    template_defs = recipe.get("templates", [])
    template_cases: List[RoutingCase] = []
    for idx, t_data in enumerate(template_defs):
        desc = t_data["description"]
        subj = t_data.get("subject")
        intent = t_data["intent"]
        acceptable = tuple(t_data.get("acceptable_intents", [intent]))
        escalate = bool(t_data["escalate"])
        decision = str(t_data["decision"])
        queue = str(t_data["queue"])
        sentiment = t_data.get("sentiment", "neutral")
        urgency = int(t_data.get("urgency", 1))

        expected = Expected(
            intent=intent,
            acceptable_intents=acceptable,
            escalate=escalate,
            decision=decision,
            queue=queue,
            sentiment=sentiment,
            urgency=urgency,
        )
        c_hash = compute_content_hash(subject=subj, description=desc)
        t_id = make_ticket_id("synthetic_template", c_hash)
        style = Style(
            register=t_data.get("register", "casual"),
            length_bucket=_compute_length_bucket(desc),
        )
        ann = Annotation(
            annotator_id="template_generator_v1",
            annotator_type="generator",
            labels=expected.to_dict(),
            confidence=1.0,
            rationale=t_data.get("notes", "Deterministic structural template"),
            pass_index=0,
            created_at="2026-10-28T00:00:00Z",
        )
        scen = t_data.get("scenario", "normal")
        case = RoutingCase(
            ticket_id=t_id,
            content_hash=c_hash,
            dataset_version=version,
            split="pool",
            created_at="2026-10-28T00:00:00Z",
            subject=subj,
            description=desc,
            channel=t_data.get("channel", "web"),
            language="en",
            pii_scrubbed=True,
            expected=expected,
            difficulty=t_data.get("difficulty", "medium"),
            scenarios=(scen,),
            style=style,
            source="synthetic",
            source_dataset="synthetic_template",
            source_ref=f"template_{scen}_{idx:03d}",
            ground_truth_source="synthetic_intended",
            label_status="provisional",
            verified_fields=(),
            annotations=(ann,),
            adjudication=None,
            group_id=f"grp-{t_id}",
            parent_ticket_id=None,
            perturbation=None,
            failure_ref=None,
            notes=t_data.get("notes", ""),
        )
        template_cases.append(case)
    all_stage1_cases.extend(template_cases)

    # 5. Deterministic perturbations from verified C2 bases
    pert_cfg = recipe.get("perturbations", {})
    verified_c2 = [c for c in c2_cases if c.label_status == "verified"]
    verified_c2.sort(key=lambda c: c.ticket_id)

    num_bases = int(pert_cfg.get("num_c2_bases", 60))
    rng_pert = random.Random(effective_seed)
    chosen_bases = rng_pert.sample(verified_c2, min(num_bases, len(verified_c2))) if verified_c2 else []
    chosen_bases.sort(key=lambda c: c.ticket_id)

    mp_types = list(pert_cfg.get("meaning_preserving_types", [
        "typos", "case", "punctuation_strip", "emoji_slang", "long_padding", "prompt_injection"
    ]))

    perturbation_cases: List[RoutingCase] = []
    # Derive 2 meaning-preserving variants per base
    for b_idx, base in enumerate(chosen_bases):
        t1 = mp_types[(b_idx * 2) % len(mp_types)]
        t2 = mp_types[(b_idx * 2 + 1) % len(mp_types)]
        p1 = perturb.derive(base, t1, seed=effective_seed + b_idx)
        p2 = perturb.derive(base, t2, seed=effective_seed + b_idx + 100)
        perturbation_cases.extend([p1, p2])

    # Truncate and conflicting variants on 20 bases
    num_altering = int(pert_cfg.get("semantic_altering_bases", 20))
    altering_bases = chosen_bases[: min(num_altering, len(chosen_bases))]
    for a_idx, base in enumerate(altering_bases):
        p_trunc = perturb.derive(base, "truncate", seed=effective_seed + 1000 + a_idx, drop_fraction=0.4)
        p_conf = perturb.derive(base, "conflicting_info", seed=effective_seed + 2000 + a_idx)
        perturbation_cases.extend([p_trunc, p_conf])

    all_stage1_cases.extend(perturbation_cases)

    # 6. Curated regression seeds
    reg_cfg = recipe.get("sources", {}).get("regression", {})
    reg_cases: List[RoutingCase] = []
    if reg_cfg:
        reg_cases = regression.load_regression_seeds(
            yaml_path=reg_cfg.get("yaml_path"),
            adversarial_path=reg_cfg.get("adversarial_path"),
            dataset_version=version,
        )

    # Deduplicate existing regression seeds to obtain clean unique count
    existing_reg_hashes = {c.content_hash for c in reg_cases}
    target_reg_count = int(recipe.get("quotas", {}).get("regression", 150))
    needed_reg = max(0, target_reg_count - len(existing_reg_hashes))

    if needed_reg > 0 and verified_c2:
        # Determine next available REG-NNNN ID
        max_reg_num = 0
        for rc in reg_cases:
            if rc.failure_ref and rc.failure_ref.failure_id:
                m = re.match(r"^REG-(\d{4})$", rc.failure_ref.failure_id)
                if m:
                    max_reg_num = max(max_reg_num, int(m.group(1)))

        english_verified_c2 = [c for c in verified_c2 if c.language == "en" and is_english(c.description)]
        rng_reg = random.Random(effective_seed + 9999)
        reg_bases = rng_reg.sample(english_verified_c2, min(needed_reg, len(english_verified_c2)))
        reg_bases.sort(key=lambda c: c.ticket_id)

        prompt_inj_reg_cases: List[RoutingCase] = []
        for idx, base in enumerate(reg_bases):
            reg_num = max_reg_num + 1 + idx
            fid = f"REG-{reg_num:04d}"
            pert_case = perturb.derive(base, "prompt_injection", seed=effective_seed + 5000 + idx)
            fref = FailureRef(
                failure_id=fid,
                observed_at="2026-10-28T00:00:00Z",
                symptom="injection must not change routing",
                root_cause="Prompt injection sensitivity; injection string must not change routing decision",
            )
            scens = list(pert_case.scenarios or ())
            if "observed_failure" not in scens:
                scens = ["observed_failure"] + scens
            if pert_case.language != "en" and "ood" not in scens:
                scens.append("ood")

            c_hash = compute_content_hash(subject=pert_case.subject, description=pert_case.description)
            t_id = make_ticket_id("observed_failure", c_hash)

            reg_case = dataclasses.replace(
                pert_case,
                ticket_id=t_id,
                content_hash=c_hash,
                split="regression",
                source="synthetic",
                source_dataset="observed_failure",
                source_ref=f"regression_{fid}_{base.ticket_id}",
                ground_truth_source="rule_derived",
                label_status="provisional",
                scenarios=tuple(scens),
                group_id=f"grp-reg-{fid}",
                failure_ref=fref,
            )
            prompt_inj_reg_cases.append(reg_case)

        reg_cases.extend(prompt_inj_reg_cases)

    all_stage1_cases.extend(reg_cases)

    # Normalize all cases to ensure schema compliance (e.g. non-English has ood)
    all_stage1_cases = [_ensure_case_schema_compliance(c) for c in all_stage1_cases]

    # Exact deduplicate initial candidate pool
    pool_stage1, exact_dropped = dedup.exact_deduplicate(all_stage1_cases)

    # Hard-fail gate: ensure all cases strictly pass schema validation
    validate_pool_cases(pool_stage1, stage_name="stage1")

    pool_stage1_path = work / "pool_stage1.jsonl"
    write_jsonl(pool_stage1_path, pool_stage1)

    # 7. Generation packets (~260 tickets across 26 packets of 10 tickets)
    gen_cfg = recipe.get("synthetic_generation", {})
    scenario_weights: Dict[str, int] = gen_cfg.get("scenario_weights", {
        "ambiguous": 50,
        "conflicting": 30,
        "missing_info": 30,
        "rare": 30,
        "ood": 40,
        "long": 30,
        "prompt_injection": 30,
        "noisy": 20,
    })
    tickets_per_packet = int(gen_cfg.get("tickets_per_packet", 10))

    taxonomy_defs = {
        "intents": {name: item.definition for name, item in tax.intent_definitions.items()},
        "sentiments": tax.sentiment_criteria,
        "urgency_levels": tax.urgency_levels,
        "decisions": list(tax.decisions),
        "queues": list(tax.queues),
    }

    gen_packets: List[Dict[str, Any]] = []
    packet_counter = 1
    canonical_intents = list(tax.intents)
    registers = ["casual", "formal", "terse", "emotional"]
    lengths = ["short (<20 words)", "medium (20–80)", "long (>80)"]

    for scenario, target_count in scenario_weights.items():
        num_packets_for_scen = max(1, target_count // tickets_per_packet)
        for p_i in range(num_packets_for_scen):
            packet_id = f"gen_packet_{packet_counter:03d}"
            packet_counter += 1

            targets: List[Dict[str, Any]] = []
            for t_idx in range(tickets_per_packet):
                global_idx = p_i * tickets_per_packet + t_idx
                # For ood scenario, intent is strictly 'other'
                if scenario == "ood":
                    target_intent = "other"
                else:
                    target_intent = canonical_intents[global_idx % len(canonical_intents)]

                is_email = (t_idx < 5)
                channel = "email" if is_email else "chat"
                reg = registers[(global_idx + t_idx) % len(registers)]
                length = "long (>80)" if scenario == "long" else lengths[(global_idx + t_idx) % len(lengths)]
                difficulty = "hard" if scenario in {"ambiguous", "conflicting", "prompt_injection"} else "medium"

                targets.append({
                    "target_id": t_idx + 1,
                    "intent": target_intent,
                    "scenario": scenario,
                    "channel": channel,
                    "style": {
                        "register": reg,
                        "length_bucket": length,
                    },
                    "difficulty": difficulty,
                    "requires_subject": is_email,
                    "notes": f"Scenario: {scenario}. Target intent: {target_intent}. Tone: {reg}.",
                })

            packet_obj = {
                "packet_id": packet_id,
                "scenario": scenario,
                "ticket_count": tickets_per_packet,
                "targets": targets,
                "instructions": (
                    f"Generate exactly {tickets_per_packet} realistic customer support tickets matching each target cell below. "
                    "For email tickets, provide a realistic subject line and body. For chat tickets, subject must be null. "
                    "All tickets must pass taxonomy validation and adhere to GUIDELINES.md label derivation rules."
                ),
                "guidelines_excerpt": guidelines_excerpt,
                "taxonomy_definitions": taxonomy_defs,
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "packet_id": {"type": "string"},
                        "tickets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "target_id": {"type": "integer"},
                                    "subject": {"type": ["string", "null"]},
                                    "description": {"type": "string"},
                                    "intended_expected": {
                                        "type": "object",
                                        "properties": {
                                            "intent": {"type": "string"},
                                            "acceptable_intents": {"type": "array", "items": {"type": "string"}},
                                            "escalate": {"type": "boolean"},
                                            "decision": {"type": "string"},
                                            "queue": {"type": "string"},
                                            "sentiment": {"type": "string"},
                                            "urgency": {"type": "integer"},
                                        },
                                        "required": ["intent", "escalate", "decision", "queue"],
                                    },
                                    "notes": {"type": "string"},
                                },
                                "required": ["description", "intended_expected"],
                            },
                        },
                    },
                    "required": ["packet_id", "tickets"],
                },
                "example": {
                    "packet_id": packet_id,
                    "tickets": [
                        {
                            "target_id": 1,
                            "subject": "Delayed replacement parcel tracking",
                            "description": "Hi support, I returned my shoes last week and was promised a replacement shipment, but I haven't received a tracking number. Could you check if the replacement order has left the warehouse?",
                            "intended_expected": {
                                "intent": "order_status",
                                "acceptable_intents": ["order_status", "returns_refunds"],
                                "escalate": False,
                                "decision": "auto_rag",
                                "queue": "none",
                                "sentiment": "neutral",
                                "urgency": 1,
                            },
                            "notes": "Boundary inquiry between order tracking and return replacement.",
                        }
                    ],
                },
            }
            gen_packets.append(packet_obj)

    gen_packets_path = work / "generation_packets.jsonl"
    with gen_packets_path.open("w", encoding="utf-8", newline="\n") as f:
        for p in gen_packets:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # 8. Paraphrase packets for 40 verified C2 bases x 3 paraphrases each
    para_cfg = recipe.get("paraphrases", {})
    num_para_bases = int(para_cfg.get("num_c2_bases", 40))
    rng_para = random.Random(effective_seed + 42)
    para_bases = rng_para.sample(verified_c2, min(num_para_bases, len(verified_c2))) if verified_c2 else []
    para_bases.sort(key=lambda c: c.ticket_id)

    para_packets: List[Dict[str, Any]] = []
    for idx, base in enumerate(para_bases, 1):
        packet_id = f"para_packet_{idx:03d}"
        para_packets.append({
            "packet_id": packet_id,
            "parent_ticket_id": base.ticket_id,
            "group_id": base.group_id or f"grp-{base.ticket_id}",
            "original_text": base.description,
            "original_subject": base.subject,
            "expected_labels": base.expected.to_dict(),
            "num_paraphrases": 3,
            "instructions": (
                "Generate 3 diverse, natural paraphrases of the customer support ticket text below while strictly "
                "preserving the customer's intent and expected routing labels. One should be casual/conversational, "
                "one formal/business tone, and one terse/brief."
            ),
            "guidelines_excerpt": guidelines_excerpt,
            "taxonomy_definitions": taxonomy_defs,
            "output_schema": {
                "type": "object",
                "properties": {
                    "packet_id": {"type": "string"},
                    "parent_ticket_id": {"type": "string"},
                    "paraphrases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "subject": {"type": ["string", "null"]},
                                "description": {"type": "string"},
                                "style_variant": {"type": "string"},
                            },
                            "required": ["description"],
                        },
                    },
                },
                "required": ["packet_id", "parent_ticket_id", "paraphrases"],
            },
            "example": {
                "packet_id": packet_id,
                "parent_ticket_id": base.ticket_id,
                "paraphrases": [
                    {"subject": None, "description": "Could someone please check the current status of order [ORDER]? Thanks!", "style_variant": "casual"},
                    {"subject": "Inquiry regarding order shipment status", "description": "Good morning. I am writing to inquire about the current transit progress of order [ORDER].", "style_variant": "formal"},
                    {"subject": None, "description": "order [ORDER] status plz", "style_variant": "terse"},
                ],
            },
        })

    para_packets_path = work / "paraphrase_packets.jsonl"
    with para_packets_path.open("w", encoding="utf-8", newline="\n") as f:
        for p in para_packets:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # 9. Compute summary counts and print coverage table
    source_counts = collections.Counter(c.source for c in pool_stage1)
    scenario_counts = collections.Counter(sc for c in pool_stage1 for sc in (c.scenarios or ()))
    intent_counts = collections.Counter(c.expected.intent for c in pool_stage1)

    print("=" * 70)
    print("STAGE 1 COMPLETED: Initial Candidate Pool and LLM Generation Packets")
    print("=" * 70)
    print(f"Total Stage 1 Candidates: {len(pool_stage1)} (Exact duplicates dropped: {exact_dropped})")
    print(f"Pool Stage 1 written to: {pool_stage1_path}")
    print("\n[Breakdown by Source]")
    for src, cnt in sorted(source_counts.items()):
        print(f"  - {src:12s}: {cnt:4d}")

    print("\n[Breakdown by Intent]")
    for it, cnt in sorted(intent_counts.items()):
        print(f"  - {it:18s}: {cnt:4d}")

    print("\n[Breakdown by Scenario in Stage 1 Pool]")
    for sc, cnt in sorted(scenario_counts.items()):
        print(f"  - {sc:18s}: {cnt:4d}")

    print("\n[Offline LLM Synthesis Targets (Coverage Gaps to be Filled)]")
    print(f"  - Generation Packets: {len(gen_packets)} packets (~{len(gen_packets) * tickets_per_packet} tickets)")
    for sc, target in sorted(scenario_weights.items()):
        curr = scenario_counts.get(sc, 0)
        gap = max(0, target - curr)
        print(f"    * {sc:18s}: target={target:2d} | current={curr:2d} | gap={gap:2d}")
    print(f"  - Paraphrase Packets: {len(para_packets)} packets ({len(para_bases) * 3} paraphrases for {len(para_bases)} bases)")
    print("=" * 70)

    return {
        "pool_stage1_count": len(pool_stage1),
        "exact_duplicates_dropped": exact_dropped,
        "pool_stage1_path": str(pool_stage1_path),
        "generation_packets_count": len(gen_packets),
        "generation_packets_path": str(gen_packets_path),
        "paraphrase_packets_count": len(para_packets),
        "paraphrase_packets_path": str(para_packets_path),
        "source_counts": dict(source_counts),
        "scenario_counts": dict(scenario_counts),
        "intent_counts": dict(intent_counts),
    }


# ---------------------------------------------------------------------------
# STAGE 2: Ingest generated tickets + paraphrases; export annotation packets
# ---------------------------------------------------------------------------


def stage2(
    work_dir: pathlib.Path | str,
    generated_dir: pathlib.Path | str,
    recipe_path: Optional[pathlib.Path | str] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute pipeline stage 2: ingest generation responses and export annotation packets.

    # REQ-REVAL-004 / REQ-REVAL-006: Ingest synthetic generation & export k-pass annotation packets.
    """
    work = pathlib.Path(work_dir)
    stage1_path = work / "pool_stage1.jsonl"
    if not stage1_path.exists():
        raise FileNotFoundError(f"pool_stage1.jsonl not found in {work}. Run stage1 first.")

    recipe = load_recipe(recipe_path=recipe_path)
    effective_seed = seed if seed is not None else int(recipe.get("seed", 20261028))
    version = str(recipe.get("dataset_version", "routing_v1.0.0"))
    tax = load_taxonomy()

    pool_stage1 = read_jsonl(stage1_path)
    parent_map = {c.ticket_id: c for c in pool_stage1}

    gen_dir = pathlib.Path(generated_dir)
    if not gen_dir.exists():
        raise FileNotFoundError(f"Generated responses directory does not exist: {gen_dir}")

    # Load generation packet definitions to map target cells (scenario, style, difficulty, channel)
    gen_packets_path = work / "generation_packets.jsonl"
    packet_targets_map: Dict[str, Dict[str, Any]] = {}
    if gen_packets_path.exists():
        with gen_packets_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    p_obj = json.loads(stripped)
                    packet_targets_map[p_obj.get("packet_id", "")] = p_obj

    newly_generated_cases: List[RoutingCase] = []
    paraphrase_cases: List[RoutingCase] = []
    total_malformed: List[Dict[str, Any]] = []

    # Scan and ingest all response JSONL files
    for resp_file in sorted(gen_dir.glob("*.jsonl")):
        content = resp_file.read_text(encoding="utf-8")
        parsed_entries, malformed_lines = _parse_json_lines_or_block(content)
        total_malformed.extend(malformed_lines)

        for entry in parsed_entries:
            # 1. Check for generation packet response format
            if "tickets" in entry and isinstance(entry["tickets"], list):
                pkt_id = entry.get("packet_id", "")
                pkt_def = packet_targets_map.get(pkt_id, {})
                pkt_targets = pkt_def.get("targets", [])
                targets_by_id = {t["target_id"]: t for t in pkt_targets if "target_id" in t}

                for item_idx, item in enumerate(entry["tickets"]):
                    if not isinstance(item, dict):
                        continue
                    desc = item.get("description", "").strip()
                    if not desc:
                        continue
                    exp_dict = item.get("intended_expected") or item.get("expected")
                    if not exp_dict or not isinstance(exp_dict, dict):
                        continue

                    target_id = item.get("target_id")
                    cell = targets_by_id.get(target_id) or (pkt_targets[item_idx] if item_idx < len(pkt_targets) else {})

                    intent = exp_dict.get("intent") or cell.get("intent") or "other"
                    acceptable = exp_dict.get("acceptable_intents") or cell.get("acceptable_intents") or [intent]
                    if intent not in acceptable:
                        acceptable = [intent] + list(acceptable)

                    escalate = bool(exp_dict.get("escalate", intent == "other"))
                    decision = str(exp_dict.get("decision", "escalate" if escalate else "auto_rag"))
                    queue = str(exp_dict.get("queue", "human-standard" if escalate else "none"))
                    sentiment = str(exp_dict.get("sentiment", "neutral"))
                    urgency = int(exp_dict.get("urgency", 1 if escalate else 0))

                    clean_exp_dict = {
                        "intent": intent,
                        "acceptable_intents": list(acceptable),
                        "escalate": escalate,
                        "decision": decision,
                        "queue": queue,
                        "sentiment": sentiment,
                        "urgency": urgency,
                    }
                    if validate_expected(clean_exp_dict):
                        total_malformed.append({"item": item, "error": "Invalid expected fields"})
                        continue

                    expected = Expected(
                        intent=intent,
                        acceptable_intents=tuple(acceptable),
                        escalate=escalate,
                        decision=decision,
                        queue=queue,
                        sentiment=sentiment,
                        urgency=urgency,
                    )
                    subj = item.get("subject")
                    c_hash = compute_content_hash(subject=subj, description=desc)
                    t_id = make_ticket_id("synthetic_jetski", c_hash)

                    target_scen = cell.get("scenario") or pkt_def.get("scenario") or item.get("scenario") or "normal"
                    scens = [target_scen]
                    if item.get("language", "en") != "en" and "ood" not in scens:
                        scens.append("ood")

                    cell_style = cell.get("style") or {}
                    reg = cell_style.get("register") or item.get("register") or "casual"
                    len_b = cell_style.get("length_bucket") or _compute_length_bucket(desc)
                    style = Style(register=reg, length_bucket=len_b)

                    diff = cell.get("difficulty") or item.get("difficulty") or "medium"
                    chan = cell.get("channel") or item.get("channel") or ("email" if subj else "chat")

                    ann = Annotation(
                        annotator_id=pkt_id or "jetski_generator",
                        annotator_type="generator",
                        labels=expected.to_dict(),
                        confidence=float(item.get("confidence", 0.95)),
                        rationale=item.get("notes", "Synthetic intended label from generator"),
                        pass_index=0,
                        created_at="2026-10-28T00:00:00Z",
                    )
                    case = RoutingCase(
                        ticket_id=t_id,
                        content_hash=c_hash,
                        dataset_version=version,
                        split="pool",
                        created_at="2026-10-28T00:00:00Z",
                        subject=subj,
                        description=desc,
                        channel=chan,
                        language=item.get("language", "en"),
                        pii_scrubbed=True,
                        expected=expected,
                        difficulty=diff,
                        scenarios=tuple(scens),
                        style=style,
                        source="synthetic",
                        source_dataset="synthetic_jetski",
                        source_ref=f"{pkt_id}_{t_id}",
                        ground_truth_source="synthetic_intended",
                        label_status="provisional",
                        verified_fields=(),
                        annotations=(ann,),
                        adjudication=None,
                        group_id=f"grp-{t_id}",
                        parent_ticket_id=None,
                        perturbation=None,
                        failure_ref=None,
                        notes=item.get("notes", ""),
                    )
                    newly_generated_cases.append(case)

            # 2. Check for paraphrase packet response format
            parent_tid = entry.get("parent_ticket_id")
            if parent_tid and "paraphrases" in entry and isinstance(entry["paraphrases"], list):
                parent_case = parent_map.get(parent_tid)
                for p_item in entry["paraphrases"]:
                    if not isinstance(p_item, dict):
                        continue
                    p_desc = p_item.get("description", "").strip()
                    if not p_desc:
                        continue
                    p_subj = p_item.get("subject")
                    c_hash = compute_content_hash(subject=p_subj, description=p_desc)
                    t_id = make_ticket_id("synthetic_jetski", c_hash)

                    p_scenarios = ["paraphrase"]
                    if parent_case and (parent_case.language != "en" or "ood" in (parent_case.scenarios or ())):
                        p_scenarios.append("ood")

                    p_style = Style(
                        register=p_item.get("style_variant") or (parent_case.style.register if parent_case else "casual"),
                        length_bucket=_compute_length_bucket(p_desc),
                    )
                    expected = parent_case.expected if parent_case else Expected(
                        intent="order_status", acceptable_intents=("order_status",), escalate=False, decision="auto_rag", queue="none", sentiment="neutral", urgency=1
                    )
                    ann = Annotation(
                        annotator_id="jetski_paraphraser",
                        annotator_type="generator",
                        labels=expected.to_dict(),
                        confidence=0.95,
                        rationale=f"Paraphrase of parent ticket {parent_tid}",
                        pass_index=0,
                        created_at="2026-10-28T00:00:00Z",
                    )
                    p_case = RoutingCase(
                        ticket_id=t_id,
                        content_hash=c_hash,
                        dataset_version=version,
                        split="pool",
                        created_at="2026-10-28T00:00:00Z",
                        subject=p_subj,
                        description=p_desc,
                        channel=parent_case.channel if parent_case else "web",
                        language=parent_case.language if parent_case else "en",
                        pii_scrubbed=True,
                        expected=expected,
                        difficulty=parent_case.difficulty if parent_case else "medium",
                        scenarios=tuple(p_scenarios),
                        style=p_style,
                        source="synthetic",
                        source_dataset="synthetic_jetski",
                        source_ref=f"para_{parent_tid}_{t_id}",
                        ground_truth_source="synthetic_intended",
                        label_status="provisional",
                        verified_fields=(),
                        annotations=(ann,),
                        adjudication=None,
                        group_id=parent_case.group_id if parent_case else f"grp-{parent_tid}",
                        parent_ticket_id=parent_tid,
                        perturbation=None,
                        failure_ref=None,
                        notes=f"paraphrase_of:{parent_tid}",
                    )
                    paraphrase_cases.append(p_case)

    # Merge into pool stage 2 and deduplicate
    combined = list(pool_stage1) + newly_generated_cases + paraphrase_cases
    combined = [_ensure_case_schema_compliance(c) for c in combined]
    pool_stage2, dedup_dropped = dedup.exact_deduplicate(combined)

    # Invariant assertion: ticket_ids must stay byte-identical to existing pool_stage2 if present
    pool_stage2_path = work / "pool_stage2.jsonl"
    if pool_stage2_path.exists():
        existing_cases = read_jsonl(pool_stage2_path)
        existing_ids = [c.ticket_id for c in existing_cases]
        new_ids = [c.ticket_id for c in pool_stage2]
        if len(existing_ids) == len(new_ids):
            assert existing_ids == new_ids, (
                "Invariant violation: ticket_ids in pool_stage2 must stay byte-identical across re-ingests"
            )

    validate_pool_cases(pool_stage2, stage_name="stage2")
    write_jsonl(pool_stage2_path, pool_stage2)

    # 3. Export k=3 annotation packets for non-verified, non-regression cases
    # Verified-intent C2 cases still need packets for decision/queue/sentiment/urgency annotation
    ann_cfg = recipe.get("annotation", {})
    k_passes = int(ann_cfg.get("k_passes", 3))
    batch_size = int(ann_cfg.get("batch_size", 20))

    cases_to_annotate: List[RoutingCase] = [
        c for c in pool_stage2
        if c.split != "regression" and c.source_dataset != "observed_failure" and "observed_failure" not in (c.scenarios or ())
    ]
    cases_to_annotate.sort(key=lambda c: c.ticket_id)

    guidelines_text = ""
    if _GUIDELINES_PATH.exists():
        guidelines_text = _GUIDELINES_PATH.read_text(encoding="utf-8")[:2500]

    taxonomy_defs = {
        "intents": {name: item.definition for name, item in tax.intent_definitions.items()},
        "sentiments": tax.sentiment_criteria,
        "urgency_levels": tax.urgency_levels,
        "decisions": list(tax.decisions),
        "queues": list(tax.queues),
    }

    packets_per_pass: Dict[int, List[Dict[str, Any]]] = {}

    for p_idx in range(k_passes):
        rng_ann = random.Random(effective_seed + p_idx * 1000 + 7)
        shuffled_intents = list(tax.intents)
        rng_ann.shuffle(shuffled_intents)
        shuffled_decisions = list(tax.decisions)
        rng_ann.shuffle(shuffled_decisions)
        shuffled_queues = list(tax.queues)
        rng_ann.shuffle(shuffled_queues)

        pass_packets: List[Dict[str, Any]] = []
        n_batches = (len(cases_to_annotate) + batch_size - 1) // batch_size

        for b_i in range(n_batches):
            batch_slice = cases_to_annotate[b_i * batch_size : (b_i + 1) * batch_size]
            pkt_id = f"ann_packet_pass{p_idx}_{b_i + 1:03d}"

            tickets_payload = []
            for c in batch_slice:
                has_verified_intent = "intent" in c.verified_fields
                t_info = {
                    "ticket_id": c.ticket_id,
                    "subject": c.subject,
                    "description": c.description,
                    "channel": c.channel,
                    "verified_fields": list(c.verified_fields),
                    "instruction": (
                        "Intent is verified ground truth; annotate decision, queue, escalate, sentiment, urgency."
                        if has_verified_intent
                        else "Annotate intent, acceptable_intents, decision, queue, escalate, sentiment, urgency."
                    ),
                }
                tickets_payload.append(t_info)

            packet_obj = {
                "packet_id": pkt_id,
                "pass_index": p_idx,
                "ticket_count": len(tickets_payload),
                "shuffled_intent_options": shuffled_intents,
                "shuffled_decision_options": shuffled_decisions,
                "shuffled_queue_options": shuffled_queues,
                "taxonomy_definitions": taxonomy_defs,
                "guidelines_excerpt": guidelines_text,
                "tickets": tickets_payload,
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "packet_id": {"type": "string"},
                        "pass_index": {"type": "integer"},
                        "annotator_id": {"type": "string"},
                        "labels": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ticket_id": {"type": "string"},
                                    "expected": {
                                        "type": "object",
                                        "properties": {
                                            "intent": {"type": "string"},
                                            "acceptable_intents": {"type": "array", "items": {"type": "string"}},
                                            "escalate": {"type": "boolean"},
                                            "decision": {"type": "string"},
                                            "queue": {"type": "string"},
                                            "sentiment": {"type": "string"},
                                            "urgency": {"type": "integer"},
                                        },
                                        "required": ["intent", "escalate", "decision", "queue"],
                                    },
                                    "confidence": {"type": "number"},
                                    "rationale": {"type": "string"},
                                },
                                "required": ["ticket_id", "expected", "confidence", "rationale"],
                            },
                        },
                    },
                    "required": ["packet_id", "pass_index", "annotator_id", "labels"],
                },
                "example": {
                    "packet_id": pkt_id,
                    "pass_index": p_idx,
                    "annotator_id": f"jetski_annotator_p{p_idx}",
                    "labels": [
                        {
                            "ticket_id": tickets_payload[0]["ticket_id"] if tickets_payload else "rt-example",
                            "expected": {
                                "intent": "order_status",
                                "acceptable_intents": ["order_status"],
                                "escalate": False,
                                "decision": "auto_rag",
                                "queue": "none",
                                "sentiment": "neutral",
                                "urgency": 1,
                            },
                            "confidence": 0.95,
                            "rationale": "Tracking request answerable by order status FAQ.",
                        }
                    ],
                },
            }
            pass_packets.append(packet_obj)

        packets_per_pass[p_idx] = pass_packets
        pass_file = work / f"annotation_packets_pass{p_idx}.jsonl"

        # Check if existing packet file has identical ticket set
        existing_matches = False
        if pass_file.exists():
            try:
                existing_tids = []
                with pass_file.open("r", encoding="utf-8") as pf:
                    for line in pf:
                        if line.strip():
                            p_data = json.loads(line)
                            for t in p_data.get("tickets", []):
                                existing_tids.append(t["ticket_id"])
                new_tids = []
                for pkt in pass_packets:
                    for t in pkt.get("tickets", []):
                        new_tids.append(t["ticket_id"])
                if existing_tids == new_tids:
                    existing_matches = True
            except Exception:
                existing_matches = False

        if not existing_matches:
            with pass_file.open("w", encoding="utf-8", newline="\n") as f:
                for pkt in pass_packets:
                    f.write(json.dumps(pkt, ensure_ascii=False) + "\n")
        else:
            print(f"Preserving existing {pass_file.name} (ticket IDs and order byte-identical).")

    print("=" * 70)
    print("STAGE 2 COMPLETED: Generated Tickets Ingested & Annotation Packets Created")
    print("=" * 70)
    print(f"Ingested Generated Cases: {len(newly_generated_cases)}")
    print(f"Ingested Paraphrases: {len(paraphrase_cases)}")
    print(f"Malformed Lines Tolerated/Skipped: {len(total_malformed)}")
    print(f"Total Cases in Pool Stage 2: {len(pool_stage2)} (Written to {pool_stage2_path})")
    print(f"Cases Scheduled for Annotation: {len(cases_to_annotate)}")
    for p_idx in range(k_passes):
        p_path = work / f"annotation_packets_pass{p_idx}.jsonl"
        print(f"  - Pass {p_idx}: {len(packets_per_pass[p_idx])} packets (Written to {p_path})")
    print("=" * 70)

    return {
        "newly_generated_count": len(newly_generated_cases),
        "paraphrases_count": len(paraphrase_cases),
        "pool_stage2_count": len(pool_stage2),
        "pool_stage2_path": str(pool_stage2_path),
        "cases_annotated_count": len(cases_to_annotate),
        "packets_per_pass": {p: len(pkts) for p, pkts in packets_per_pass.items()},
    }


# ---------------------------------------------------------------------------
# STAGE 2b: Ingest fresh real customer threads & export fresh annotation packets
# ---------------------------------------------------------------------------


def stage2b(
    work_dir: pathlib.Path | str,
    recipe_path: Optional[pathlib.Path | str] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute pipeline stage 2b: sample fresh real threads and export new annotation packets.

    # REQ-REVAL-004 / REQ-REVAL-006: Ingest fresh real Twitter customer threads.
    - Reads pool_stage2.jsonl.
    - Samples fresh real threads from raw threads.jsonl excluding Laya hashes and pool_stage2 hashes.
    - Appends fresh real cases to pool_stage2 -> pool_stage2b.jsonl.
    - Hard-fail validates pool_stage2b cases.
    - Exports annotation packets for ONLY new cases to annotation_packets_fresh_pass{0,1,2}.jsonl.
    - NEVER touches existing annotation_packets_pass*.jsonl files.
    """
    work = pathlib.Path(work_dir)
    stage2_path = work / "pool_stage2.jsonl"
    if not stage2_path.exists():
        raise FileNotFoundError(f"pool_stage2.jsonl not found in {work}. Run stage2 first.")

    recipe = load_recipe(recipe_path=recipe_path)
    effective_seed = seed if seed is not None else int(recipe.get("seed", 20261028))
    version = str(recipe.get("dataset_version", "routing_v1.0.0"))
    tax = load_taxonomy()

    pool_stage2 = read_jsonl(stage2_path)

    # Contamination hashes (train_val + test) + existing pool hashes
    contamination_cfg = recipe.get("contamination", {})
    laya_hashes_path = pathlib.Path(contamination_cfg.get("laya_hashes_path", ".cache/routing_eval/laya_hashes.json"))
    exclude_hashes: Set[str] = set()
    if laya_hashes_path.exists():
        with laya_hashes_path.open("r", encoding="utf-8") as f:
            h_data = json.load(f)
        exclude_hashes.update(h_data.get("train_val", []))
        exclude_hashes.update(h_data.get("test", []))

    for c in pool_stage2:
        exclude_hashes.add(c.content_hash)
        exclude_hashes.add(compute_content_hash(subject=c.subject, description=c.description))

    # Sample fresh real threads
    fresh_cfg = recipe.get("sources", {}).get("real_fresh", {})
    threads_path = fresh_cfg.get("threads_path", ".cache/routing_eval/raw/threads.jsonl")
    n_fresh = int(fresh_cfg.get("n", 320))

    fresh_cases = real_twcs.sample_fresh_threads(
        threads_jsonl=threads_path,
        n=n_fresh,
        seed=effective_seed,
        exclude_hashes=exclude_hashes,
        dataset_version=version,
    )

    combined = list(pool_stage2) + fresh_cases
    combined = [_ensure_case_schema_compliance(c) for c in combined]
    pool_stage2b, dedup_dropped = dedup.exact_deduplicate(combined)

    # Invariant: all existing ticket_ids in pool_stage2 must stay byte-identical and in place
    p2_ids = [c.ticket_id for c in pool_stage2]
    p2b_prefix_ids = [c.ticket_id for c in pool_stage2b if c.ticket_id in set(p2_ids)]
    assert p2b_prefix_ids == p2_ids, "Invariant violation: existing pool_stage2 cases must remain byte-identical"

    validate_pool_cases(pool_stage2b, stage_name="stage2b")

    pool_stage2b_path = work / "pool_stage2b.jsonl"
    write_jsonl(pool_stage2b_path, pool_stage2b)

    # Export annotation packets for ONLY new fresh cases
    ann_cfg = recipe.get("annotation", {})
    k_passes = int(ann_cfg.get("k_passes", 3))
    batch_size = int(ann_cfg.get("batch_size", 20))

    cases_to_annotate = sorted(fresh_cases, key=lambda c: c.ticket_id)

    guidelines_text = ""
    if _GUIDELINES_PATH.exists():
        guidelines_text = _GUIDELINES_PATH.read_text(encoding="utf-8")[:2500]

    taxonomy_defs = {
        "intents": {name: item.definition for name, item in tax.intent_definitions.items()},
        "sentiments": tax.sentiment_criteria,
        "urgency_levels": tax.urgency_levels,
        "decisions": list(tax.decisions),
        "queues": list(tax.queues),
    }

    packets_per_pass: Dict[int, List[Dict[str, Any]]] = {}

    for p_idx in range(k_passes):
        rng_ann = random.Random(effective_seed + p_idx * 1000 + 777)
        shuffled_intents = list(tax.intents)
        rng_ann.shuffle(shuffled_intents)
        shuffled_decisions = list(tax.decisions)
        rng_ann.shuffle(shuffled_decisions)
        shuffled_queues = list(tax.queues)
        rng_ann.shuffle(shuffled_queues)

        pass_packets: List[Dict[str, Any]] = []
        n_batches = (len(cases_to_annotate) + batch_size - 1) // batch_size

        for b_i in range(n_batches):
            batch_slice = cases_to_annotate[b_i * batch_size : (b_i + 1) * batch_size]
            pkt_id = f"ann_packet_fresh_pass{p_idx}_{b_i + 1:03d}"

            tickets_payload = []
            for c in batch_slice:
                tickets_payload.append({
                    "ticket_id": c.ticket_id,
                    "subject": c.subject,
                    "description": c.description,
                    "channel": c.channel,
                    "verified_fields": list(c.verified_fields),
                    "instruction": "Annotate intent, acceptable_intents, decision, queue, escalate, sentiment, urgency.",
                })

            packet_obj = {
                "packet_id": pkt_id,
                "pass_index": p_idx,
                "ticket_count": len(tickets_payload),
                "shuffled_intent_options": shuffled_intents,
                "shuffled_decision_options": shuffled_decisions,
                "shuffled_queue_options": shuffled_queues,
                "taxonomy_definitions": taxonomy_defs,
                "guidelines_excerpt": guidelines_text,
                "tickets": tickets_payload,
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "packet_id": {"type": "string"},
                        "pass_index": {"type": "integer"},
                        "annotator_id": {"type": "string"},
                        "labels": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ticket_id": {"type": "string"},
                                    "expected": {
                                        "type": "object",
                                        "properties": {
                                            "intent": {"type": "string"},
                                            "acceptable_intents": {"type": "array", "items": {"type": "string"}},
                                            "escalate": {"type": "boolean"},
                                            "decision": {"type": "string"},
                                            "queue": {"type": "string"},
                                            "sentiment": {"type": "string"},
                                            "urgency": {"type": "integer"},
                                        },
                                        "required": ["intent", "escalate", "decision", "queue"],
                                    },
                                    "confidence": {"type": "number"},
                                    "rationale": {"type": "string"},
                                },
                                "required": ["ticket_id", "expected", "confidence", "rationale"],
                            },
                        },
                    },
                    "required": ["packet_id", "pass_index", "annotator_id", "labels"],
                },
                "example": {
                    "packet_id": pkt_id,
                    "pass_index": p_idx,
                    "annotator_id": f"jetski_annotator_p{p_idx}",
                    "labels": [
                        {
                            "ticket_id": tickets_payload[0]["ticket_id"] if tickets_payload else "rt-example",
                            "expected": {
                                "intent": "order_status",
                                "acceptable_intents": ["order_status"],
                                "escalate": False,
                                "decision": "auto_rag",
                                "queue": "none",
                                "sentiment": "neutral",
                                "urgency": 1,
                            },
                            "confidence": 0.95,
                            "rationale": "Tracking request answerable by order status FAQ.",
                        }
                    ],
                },
            }
            pass_packets.append(packet_obj)

        packets_per_pass[p_idx] = pass_packets
        pass_file = work / f"annotation_packets_fresh_pass{p_idx}.jsonl"

        with pass_file.open("w", encoding="utf-8", newline="\n") as f:
            for pkt in pass_packets:
                f.write(json.dumps(pkt, ensure_ascii=False) + "\n")

    print("=" * 70)
    print("STAGE 2b COMPLETED: Fresh Real Tweets Ingested & Fresh Packets Created")
    print("=" * 70)
    print(f"Sampled Fresh Real Cases: {len(fresh_cases)}")
    print(f"Total Cases in Pool Stage 2b: {len(pool_stage2b)} (Written to {pool_stage2b_path})")
    print(f"Cases Scheduled for Annotation: {len(cases_to_annotate)}")
    for p_idx in range(k_passes):
        p_path = work / f"annotation_packets_fresh_pass{p_idx}.jsonl"
        print(f"  - Pass {p_idx}: {len(packets_per_pass[p_idx])} fresh packets (Written to {p_path})")
    print("=" * 70)

    return {
        "fresh_cases_count": len(fresh_cases),
        "pool_stage2b_count": len(pool_stage2b),
        "pool_stage2b_path": str(pool_stage2b_path),
        "cases_annotated_count": len(cases_to_annotate),
        "packets_per_pass": {p: len(pkts) for p, pkts in packets_per_pass.items()},
    }


# ---------------------------------------------------------------------------
# STAGE 2c: Sample additional fresh English tickets & export fresh2 packets
# ---------------------------------------------------------------------------


def stage2c(
    work_dir: pathlib.Path | str,
    recipe_path: Optional[pathlib.Path | str] = None,
    seed: Optional[int] = None,
    n_sample: int = 40,
) -> Dict[str, Any]:
    """Sample additional fresh English real customer tickets and export fresh2 annotation packets.

    # REQ-REVAL-004 / REQ-REVAL-006: English-only fresh ticket sampling & annotation packets.
    """
    work = pathlib.Path(work_dir)
    recipe = load_recipe(recipe_path=recipe_path)
    effective_seed = seed if seed is not None else int(recipe.get("seed", 20261028)) + 1
    version = recipe.get("dataset_version", "routing_v2.0.0")

    # Load latest stage2 pool (not pool_stage3) to deduplicate
    pool_candidates = [
        work / "pool_stage2b.jsonl",
        work / "pool_stage2.jsonl",
        work / "pool_stage1.jsonl",
    ]
    existing_pools = [p for p in pool_candidates if p.exists()]
    if not existing_pools:
        raise FileNotFoundError(f"No pool stage files found in {work}")
    latest_pool_path = max(existing_pools, key=lambda p: p.stat().st_mtime)
    current_pool = read_jsonl(latest_pool_path)
    existing_hashes = {c.content_hash for c in current_pool}

    # Load Laya hashes
    contamination_cfg = recipe.get("contamination", {})
    laya_hashes_path = pathlib.Path(contamination_cfg.get("laya_hashes_path", ".cache/routing_eval/laya_hashes.json"))
    laya_hashes: Set[str] = set()
    if laya_hashes_path.exists():
        with laya_hashes_path.open("r", encoding="utf-8") as f:
            h_data = json.load(f)
        laya_hashes.update(h_data.get("train_val", []))
        laya_hashes.update(h_data.get("test", []))

    all_exclude = existing_hashes | laya_hashes

    # Source config
    fresh_cfg = recipe.get("sources", {}).get("real_fresh", {})
    threads_path = pathlib.Path(fresh_cfg.get("threads_path", ".cache/routing_eval/raw/threads.jsonl"))

    new_fresh_cases = real_twcs.sample_fresh_threads(
        threads_jsonl=threads_path,
        n=n_sample,
        seed=effective_seed,
        exclude_hashes=all_exclude,
        dataset_version=version,
        english_only=True,
    )

    # Export annotation packets fresh2 (pass 0..2, 20 cases per packet)
    k_passes = 3
    packet_size = 20
    guidelines_text = _GUIDELINES_PATH.read_text(encoding="utf-8") if _GUIDELINES_PATH.exists() else ""
    tax = load_taxonomy()
    taxonomy_defs = {
        "intents": tax.intents,
        "decisions": ["auto_rag", "escalate"],
        "queues": ["none", "human-standard", "human-urgent"],
        "sentiments": ["neutral", "frustrated", "angry", "positive"],
        "urgencies": [0, 1, 2, 3],
    }

    packets_per_pass: Dict[int, List[Dict[str, Any]]] = {}
    for p_idx in range(k_passes):
        pass_packets = []
        num_batches = (len(new_fresh_cases) + packet_size - 1) // packet_size
        for b_idx in range(num_batches):
            batch = new_fresh_cases[b_idx * packet_size : (b_idx + 1) * packet_size]
            pkt_id = f"ann_fresh2_p{p_idx}_b{b_idx:02d}"

            seed_hash = int(hashlib.md5(f"{effective_seed}:{pkt_id}:{p_idx}".encode()).hexdigest()[:8], 16)
            rng_p = random.Random(seed_hash)

            shuffled_intents = list(tax.intents)
            rng_p.shuffle(shuffled_intents)
            shuffled_decisions = ["auto_rag", "escalate"]
            rng_p.shuffle(shuffled_decisions)
            shuffled_queues = ["none", "human-standard", "human-urgent"]
            rng_p.shuffle(shuffled_queues)

            tickets_payload = [
                {
                    "ticket_id": c.ticket_id,
                    "subject": c.subject,
                    "description": c.description,
                    "channel": c.channel,
                    "verified_fields": list(c.verified_fields),
                    "instruction": "Annotate intent, acceptable_intents, decision, queue, escalate, sentiment, urgency.",
                }
                for c in batch
            ]

            packet_obj = {
                "packet_id": pkt_id,
                "pass_index": p_idx,
                "ticket_count": len(tickets_payload),
                "shuffled_intent_options": shuffled_intents,
                "shuffled_decision_options": shuffled_decisions,
                "shuffled_queue_options": shuffled_queues,
                "taxonomy_definitions": taxonomy_defs,
                "guidelines_excerpt": guidelines_text,
                "tickets": tickets_payload,
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "packet_id": {"type": "string"},
                        "pass_index": {"type": "integer"},
                        "annotator_id": {"type": "string"},
                        "labels": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ticket_id": {"type": "string"},
                                    "expected": {
                                        "type": "object",
                                        "properties": {
                                            "intent": {"type": "string"},
                                            "acceptable_intents": {"type": "array", "items": {"type": "string"}},
                                            "escalate": {"type": "boolean"},
                                            "decision": {"type": "string"},
                                            "queue": {"type": "string"},
                                            "sentiment": {"type": "string"},
                                            "urgency": {"type": "integer"},
                                        },
                                        "required": ["intent", "escalate", "decision", "queue"],
                                    },
                                    "confidence": {"type": "number"},
                                    "rationale": {"type": "string"},
                                },
                                "required": ["ticket_id", "expected", "confidence", "rationale"],
                            },
                        },
                    },
                    "required": ["packet_id", "pass_index", "annotator_id", "labels"],
                },
                "example": {
                    "packet_id": pkt_id,
                    "pass_index": p_idx,
                    "annotator_id": f"jetski_annotator_p{p_idx}",
                    "labels": [
                        {
                            "ticket_id": tickets_payload[0]["ticket_id"] if tickets_payload else "rt-example",
                            "expected": {
                                "intent": "order_status",
                                "acceptable_intents": ["order_status"],
                                "escalate": False,
                                "decision": "auto_rag",
                                "queue": "none",
                                "sentiment": "neutral",
                                "urgency": 1,
                            },
                            "confidence": 0.95,
                            "rationale": "Tracking request answerable by order status FAQ.",
                        }
                    ],
                },
            }
            pass_packets.append(packet_obj)

        packets_per_pass[p_idx] = pass_packets
        pass_file = work / f"annotation_packets_fresh2_pass{p_idx}.jsonl"
        with pass_file.open("w", encoding="utf-8", newline="\n") as f:
            for pkt in pass_packets:
                f.write(json.dumps(pkt, ensure_ascii=False) + "\n")

    # Combine into updated pool
    updated_pool = list(current_pool) + list(new_fresh_cases)
    seen_ids = set()
    deduped_pool = []
    for c in updated_pool:
        if c.ticket_id not in seen_ids:
            seen_ids.add(c.ticket_id)
            deduped_pool.append(c)

    deduped_pool = [_ensure_case_schema_compliance(c) for c in deduped_pool]
    validate_pool_cases(deduped_pool, stage_name="stage2c")

    pool_stage2c_path = work / "pool_stage2b.jsonl"
    write_jsonl(pool_stage2c_path, deduped_pool)

    print("=" * 70)
    print("STAGE 2c COMPLETED: Fresh2 Real Tweets Ingested & Fresh2 Packets Created")
    print("=" * 70)
    print(f"Sampled Fresh Real English Cases: {len(new_fresh_cases)}")
    print(f"Total Cases in Pool Stage 2b: {len(deduped_pool)} (Written to {pool_stage2c_path})")
    for p_idx in range(k_passes):
        p_path = work / f"annotation_packets_fresh2_pass{p_idx}.jsonl"
        print(f"  - Pass {p_idx}: {len(packets_per_pass[p_idx])} fresh2 packets (Written to {p_path})")
    print("=" * 70)

    return {
        "new_fresh_count": len(new_fresh_cases),
        "total_pool_count": len(deduped_pool),
        "pool_stage2b_path": str(pool_stage2c_path),
        "packets_per_pass": {p: len(pkts) for p, pkts in packets_per_pass.items()},
    }


# ---------------------------------------------------------------------------
# STAGE 3: Ingest k-pass annotations, compute reliability, write adjudication queue
# ---------------------------------------------------------------------------


def stage3(
    work_dir: pathlib.Path | str,
    annotations_dir: pathlib.Path | str | Sequence[pathlib.Path | str],
    recipe_path: Optional[pathlib.Path | str] = None,
) -> Dict[str, Any]:
    """Execute pipeline stage 3: ingest annotations, compute consensus, and queue adjudication.

    # REQ-REVAL-006 / REQ-REVAL-007: k-pass reliability, consensus, and adjudication queue.
    """
    work = pathlib.Path(work_dir)
    stage2_path = work / "pool_stage2b.jsonl"
    if not stage2_path.exists():
        stage2_path = work / "pool_stage2.jsonl"
    if not stage2_path.exists():
        raise FileNotFoundError(f"Neither pool_stage2b.jsonl nor pool_stage2.jsonl found in {work}. Run stage2/stage2b first.")

    pool_stage2 = read_jsonl(stage2_path)
    cases_by_id = {c.ticket_id: c for c in pool_stage2}

    # Resolve all annotation JSONL files across paths, comma-separated lists, and globs
    ann_files: List[pathlib.Path] = []
    seen_files: Set[pathlib.Path] = set()
    if isinstance(annotations_dir, (list, tuple)):
        raw_dirs = list(annotations_dir)
    else:
        raw_dirs = [s.strip() for s in str(annotations_dir).split(",") if s.strip()]

    for d_str in raw_dirs:
        p = pathlib.Path(d_str)
        matched_in_d: List[pathlib.Path] = []
        if p.is_file() and p.suffix == ".jsonl":
            matched_in_d.append(p)
        elif p.is_dir():
            matched_in_d.extend(sorted(p.glob("*.jsonl")))
        else:
            # Handle globs
            parent = p.parent if str(p.parent) != "" else pathlib.Path(".")
            matched_in_d.extend(sorted(parent.glob(p.name)))

        for f in matched_in_d:
            if f not in seen_files:
                seen_files.add(f)
                ann_files.append(f)

    if not ann_files:
        raise FileNotFoundError(f"No annotation JSONL files found matching: {annotations_dir}")

    passes_by_ticket: Dict[str, Dict[int, Dict[str, Any]]] = collections.defaultdict(dict)
    malformed_count = 0

    # Read all annotation response files
    for ann_file in ann_files:
        content = ann_file.read_text(encoding="utf-8")
        parsed_entries, malformed = _parse_json_lines_or_block(content)
        malformed_count += len(malformed)

        for entry in parsed_entries:
            p_idx = int(entry.get("pass_index", 0))
            annotator_id = str(entry.get("annotator_id", f"jetski_annotator_p{p_idx}"))
            label_items = entry.get("labels", [])
            if not isinstance(label_items, list):
                continue

            for lbl in label_items:
                if not isinstance(lbl, dict):
                    continue
                t_id = lbl.get("ticket_id")
                if not t_id or t_id not in cases_by_id:
                    continue

                exp_dict = lbl.get("expected") or lbl.get("labels")
                if not exp_dict or not isinstance(exp_dict, dict):
                    continue

                exp_dict.setdefault("acceptable_intents", [exp_dict.get("intent", "other")])
                if "escalate" not in exp_dict and "decision" in exp_dict:
                    exp_dict["escalate"] = (exp_dict.get("decision") == "escalate")

                if validate_expected(exp_dict):
                    malformed_count += 1
                    continue

                passes_by_ticket[t_id][p_idx] = {
                    "pass_index": p_idx,
                    "annotator_id": annotator_id,
                    "labels": exp_dict,
                    "confidence": float(lbl.get("confidence", 0.9)),
                    "rationale": str(lbl.get("rationale", "")),
                }

    # Update cases with annotation passes and compute consensus
    updated_cases: List[RoutingCase] = []
    for t_id, case in cases_by_id.items():
        case_passes_dict = passes_by_ticket.get(t_id)
        if not case_passes_dict:
            updated_cases.append(case)
            continue

        case_passes = list(case_passes_dict.values())
        case_passes.sort(key=lambda x: x["pass_index"])

        overridden_passes = {p["pass_index"] for p in case_passes}
        retained_annotations = [
            a for a in case.annotations
            if not (a.annotator_type == "llm" and a.pass_index in overridden_passes)
        ]
        new_annotations = list(retained_annotations)
        for p in case_passes:
            new_annotations.append(
                Annotation(
                    annotator_id=p["annotator_id"],
                    annotator_type="llm",
                    labels=p["labels"],
                    confidence=p["confidence"],
                    rationale=p["rationale"],
                    pass_index=p["pass_index"],
                    created_at="2026-10-28T00:00:00Z",
                )
            )

        intents = [p["labels"]["intent"] for p in case_passes]
        decisions = [p["labels"]["decision"] for p in case_passes]
        queues = [p["labels"]["queue"] for p in case_passes]
        sentiments = [p["labels"].get("sentiment") for p in case_passes if p["labels"].get("sentiment")]
        urgencies = [p["labels"].get("urgency") for p in case_passes if p["labels"].get("urgency") is not None]

        consensus_dec = collections.Counter(decisions).most_common(1)[0][0]
        consensus_q = collections.Counter(queues).most_common(1)[0][0]
        consensus_esc = (consensus_dec == "escalate")
        consensus_sent = collections.Counter(sentiments).most_common(1)[0][0] if sentiments else case.expected.sentiment
        consensus_urg = collections.Counter(urgencies).most_common(1)[0][0] if urgencies else case.expected.urgency

        # Invariant: Verified fields (e.g. C2 verified intent) MUST NOT be overwritten by annotations!
        is_fresh_unlabeled = "fresh_unlabeled" in (case.notes or "")
        if "intent" in case.verified_fields:
            final_intent = case.expected.intent
            final_acceptable = case.expected.acceptable_intents
            final_gt_source = case.ground_truth_source
            final_label_status = case.label_status
            final_verified = case.verified_fields
        else:
            final_intent = collections.Counter(intents).most_common(1)[0][0]
            final_acceptable = (final_intent,)
            final_gt_source = "llm_provisional"
            final_label_status = "provisional"
            final_verified = ()

        updated_notes = case.notes or ""
        if is_fresh_unlabeled:
            updated_notes = updated_notes.replace("fresh_unlabeled", "fresh_llm_annotated")

        updated_expected = Expected(
            intent=final_intent,
            acceptable_intents=final_acceptable,
            escalate=consensus_esc,
            decision=consensus_dec,
            queue=consensus_q,
            sentiment=consensus_sent,
            urgency=consensus_urg,
        )

        updated_cases.append(
            RoutingCase(
                ticket_id=case.ticket_id,
                content_hash=case.content_hash,
                dataset_version=case.dataset_version,
                split=case.split,
                created_at=case.created_at,
                subject=case.subject,
                description=case.description,
                channel=case.channel,
                language=case.language,
                pii_scrubbed=case.pii_scrubbed,
                expected=updated_expected,
                difficulty=case.difficulty,
                scenarios=case.scenarios,
                style=case.style,
                source=case.source,
                source_dataset=case.source_dataset,
                source_ref=case.source_ref,
                ground_truth_source=final_gt_source,
                label_status=final_label_status,
                verified_fields=final_verified,
                annotations=tuple(new_annotations),
                adjudication=case.adjudication,
                group_id=case.group_id,
                parent_ticket_id=case.parent_ticket_id,
                perturbation=case.perturbation,
                failure_ref=case.failure_ref,
                notes=updated_notes,
            )
        )

    # Fix 1b: Inherit parent decisions for meaning-preserving children
    updated_by_id = {c.ticket_id: c for c in updated_cases}
    final_cases: List[RoutingCase] = []
    for c in updated_cases:
        if (
            c.parent_ticket_id
            and c.parent_ticket_id in updated_by_id
        ):
            is_meaning_preserving = (
                "paraphrase" in (c.scenarios or ())
                or (c.perturbation and c.perturbation.type not in ("truncate", "conflicting_info"))
            )
            if is_meaning_preserving:
                parent = updated_by_id[c.parent_ticket_id]
                new_intent = parent.expected.intent if "intent" not in c.verified_fields else c.expected.intent
                new_acceptable = (
                    parent.expected.acceptable_intents
                    if "intent" not in c.verified_fields
                    else c.expected.acceptable_intents
                )
                inherited_expected = Expected(
                    intent=new_intent,
                    acceptable_intents=new_acceptable,
                    escalate=parent.expected.escalate,
                    decision=parent.expected.decision,
                    queue=parent.expected.queue,
                    sentiment=parent.expected.sentiment,
                    urgency=parent.expected.urgency,
                )
                inherited_notes = c.notes or ""
                if "inherited_from_parent" not in inherited_notes:
                    inherited_notes = (
                        f"{inherited_notes}; inherited_from_parent={parent.ticket_id}"
                        if inherited_notes
                        else f"inherited_from_parent={parent.ticket_id}"
                    )
                c = RoutingCase(
                    ticket_id=c.ticket_id,
                    content_hash=c.content_hash,
                    dataset_version=c.dataset_version,
                    split=c.split,
                    created_at=c.created_at,
                    subject=c.subject,
                    description=c.description,
                    channel=c.channel,
                    language=c.language,
                    pii_scrubbed=c.pii_scrubbed,
                    expected=inherited_expected,
                    difficulty=c.difficulty,
                    scenarios=c.scenarios,
                    style=c.style,
                    source=c.source,
                    source_dataset=c.source_dataset,
                    source_ref=c.source_ref,
                    ground_truth_source=parent.ground_truth_source,
                    label_status=parent.label_status if "intent" not in c.verified_fields else c.label_status,
                    verified_fields=c.verified_fields,
                    annotations=c.annotations,
                    adjudication=c.adjudication,
                    group_id=c.group_id,
                    parent_ticket_id=c.parent_ticket_id,
                    perturbation=c.perturbation,
                    failure_ref=c.failure_ref,
                    notes=inherited_notes,
                )
        final_cases.append(c)
    updated_cases = final_cases

    updated_cases = [_ensure_case_schema_compliance(c) for c in updated_cases]
    updated_cases.sort(key=lambda c: c.ticket_id)
    validate_pool_cases(updated_cases, stage_name="stage3")
    pool_stage3_path = work / "pool_stage3.jsonl"
    write_jsonl(pool_stage3_path, updated_cases)

    # Compute k-pass reliability metrics
    rel_report = annotate.reliability_report(updated_cases)

    # Queue cases for adjudication per REQ-REVAL-006 triggers
    queued_cases = annotate.queue_for_adjudication(updated_cases)
    adj_queue_path = work / "adjudication_queue.tsv"
    adjudicate.export_queue(queued_cases, adj_queue_path)

    # Count queue reasons
    queue_reasons = collections.Counter(reason for _, reason in queued_cases)

    print("=" * 70)
    print("STAGE 3 COMPLETED: Annotations Ingested, Reliability Computed, Queue Exported")
    print("=" * 70)
    print(f"Total Cases in Pool Stage 3: {len(updated_cases)}")
    print(f"Cases with Multi-pass LLM Annotations: {rel_report.get('cases_evaluated', 0)}")
    print(f"Adjudication Queue Written to: {adj_queue_path} ({len(queued_cases)} cases)")
    print("\n[k-Pass Inter-Annotator Reliability (Krippendorff's alpha)]")
    for f_name, f_data in rel_report.get("fields", {}).items():
        alpha = f_data.get("alpha")
        alpha_str = f"{alpha:.4f}" if alpha is not None else "N/A"
        p_agree = f_data.get("mean_pairwise_agreement")
        p_agree_str = f"{p_agree:.4f}" if p_agree is not None else "N/A"
        flag = " [UNRELIABLE < 0.67]" if f_data.get("unreliable") else ""
        print(f"  - {f_name:12s}: alpha={alpha_str} | pairwise_agreement={p_agree_str}{flag}")

    print("\n[Adjudication Queue Triggers Summary]")
    for r_reason, r_count in sorted(queue_reasons.items(), key=lambda x: -x[1]):
        print(f"  - {r_count:3d} cases: {r_reason}")
    print("=" * 70)

    return {
        "pool_stage3_count": len(updated_cases),
        "pool_stage3_path": str(pool_stage3_path),
        "reliability_report": rel_report,
        "adjudication_queue_count": len(queued_cases),
        "adjudication_queue_path": str(adj_queue_path),
        "queue_reasons": dict(queue_reasons),
    }


# ---------------------------------------------------------------------------
# BUILD: Final dataset version assembly
# ---------------------------------------------------------------------------


def build(
    work_dir: pathlib.Path | str,
    version: Optional[str] = None,
    adjudication_tsv: Optional[pathlib.Path | str] = None,
    out_root: pathlib.Path | str = "evals/data",
    recipe_path: Optional[pathlib.Path | str] = None,
    seed: Optional[int] = None,
    dry_run: bool = False,
) -> pathlib.Path | Dict[str, Any]:
    """Execute final dataset build from staged candidate pool.

    # REQ-REVAL-004 / REQ-REVAL-008 / REQ-REVAL-009 / REQ-REVAL-020:
    """
    work = pathlib.Path(work_dir)
    recipe = load_recipe(recipe_path=recipe_path)
    effective_seed = seed if seed is not None else int(recipe.get("seed", 20261028))
    effective_version = version or recipe.get("dataset_version", "routing_v2.0.0")
    quotas = recipe.get("quotas", {"dev": 400, "regression": 150, "heldout": 400})
    split_mix = recipe.get("split_mix")

    # Pick latest stage pool file by mtime
    pool_candidates = [
        work / "pool_stage3.jsonl",
        work / "pool_stage2b.jsonl",
        work / "pool_stage2.jsonl",
        work / "pool_stage1.jsonl",
    ]
    existing_pools = [p for p in pool_candidates if p.exists()]
    if not existing_pools:
        raise FileNotFoundError(f"No pool_stage*.jsonl files found in {work}. Run stage1/stage2/stage2b/stage3 first.")
    pool_path = max(existing_pools, key=lambda p: p.stat().st_mtime)

    cases = read_jsonl(pool_path)
    cases = [_ensure_case_schema_compliance(c) for c in cases]

    # Ingest adjudication decisions if provided
    if adjudication_tsv:
        adj_path = pathlib.Path(adjudication_tsv)
        if adj_path.exists():
            cases, adj_report = adjudicate.ingest_decisions(adj_path, cases, supersede=True)
            cases = [_ensure_case_schema_compliance(c) for c in cases]
            print(f"Ingested adjudication decisions from {adj_path}: {adj_report}")

    # Hard-fail gate: ensure all cases strictly pass schema validation
    validate_pool_cases(cases, stage_name="build")

    # Hard-fail gate: no English fresh case may remain 'fresh_unlabeled' at build time
    unlabeled_en_fresh = [
        c.ticket_id for c in cases
        if c.language == "en" and "fresh_unlabeled" in (c.notes or "")
    ]
    if unlabeled_en_fresh:
        raise ValueError(
            f"Build aborted: {len(unlabeled_en_fresh)} English fresh cases still have 'fresh_unlabeled' notes "
            f"(e.g. {unlabeled_en_fresh[:5]}). Run stage3 with all annotation directories before build."
        )

    # Load contamination hashes (train_val + test)
    contamination_cfg = recipe.get("contamination", {})
    laya_hashes_path = pathlib.Path(contamination_cfg.get("laya_hashes_path", ".cache/routing_eval/laya_hashes.json"))
    train_hashes: Set[str] = set()
    if laya_hashes_path.exists():
        with laya_hashes_path.open("r", encoding="utf-8") as f:
            h_data = json.load(f)
        train_hashes.update(h_data.get("train_val", []))
        train_hashes.update(h_data.get("test", []))

    if dry_run:
        from evals.routing.dedup import cluster_near_duplicates, exact_deduplicate
        from evals.routing.split import assign as split_assign

        deduped, _ = exact_deduplicate(cases)
        clustered, _ = cluster_near_duplicates(deduped, seed=effective_seed)
        assigned = split_assign(
            clustered,
            seed=effective_seed,
            quotas=quotas,
            split_mix=split_mix,
            train_hashes=train_hashes,
        )
        split_counts = collections.Counter(c.split for c in assigned)
        by_split_source: Dict[str, Dict[str, int]] = collections.defaultdict(lambda: collections.defaultdict(int))

        for c in assigned:
            src_lbl = c.source
            if c.source == "real" and ("fresh_unlabeled" in (c.notes or "") or "fresh_llm_annotated" in (c.notes or "") or (c.source_ref and c.source_ref.startswith("twcs_thread_"))):
                src_lbl = "real_fresh"
            elif c.source == "real":
                src_lbl = "contaminated_c2"
            elif c.source == "public" or c.source_dataset == "bitext_customer_support":
                src_lbl = "bitext"
            elif c.source == "synthetic" and (c.parent_ticket_id is not None or "paraphrase" in (c.scenarios or ()) or c.perturbation is not None):
                src_lbl = "paraphrase_perturbation"
            elif c.source == "synthetic" and c.source_dataset == "synthetic_template":
                src_lbl = "synthetic_template"
            elif c.source == "synthetic" and c.source_dataset == "synthetic_jetski":
                src_lbl = "synthetic_jetski"

            by_split_source[c.split][src_lbl] += 1

        print("=" * 70)
        print("DRY-RUN BUILD SPLIT COMPOSITION SUMMARY")
        print("=" * 70)
        print(f"Total pool cases evaluated: {len(assigned)}")
        for s in ("dev", "heldout", "regression", "pool"):
            if s in split_counts:
                print(f"\nSplit: {s} (Total: {split_counts[s]})")
                for src_lbl, cnt in sorted(by_split_source[s].items()):
                    print(f"  - {src_lbl:25s}: {cnt:4d}")

        heldout_collisions = [
            c for c in assigned
            if c.split == "heldout" and (
                c.content_hash in train_hashes
                or compute_content_hash(c.subject, c.description) in train_hashes
                or "laya_train_collision" in (c.notes or "")
            )
        ]
        print(f"\nHeldout Laya collisions: {len(heldout_collisions)}")
        print("=" * 70)
        return {
            "dry_run": True,
            "total_cases": len(assigned),
            "split_counts": dict(split_counts),
            "by_split_source": {s: dict(srcs) for s, srcs in by_split_source.items()},
            "heldout_collisions": len(heldout_collisions),
        }

    # Build version directory using core build orchestrator
    out_dir = build_module.build_version(
        version=effective_version,
        inputs_cfg={
            "cases": cases,
            "synthetic_templates": False,
            "regression": {},
            "train_hashes": list(train_hashes),
            "split_mix": split_mix,
        },
        seed=effective_seed,
        out_root=out_root,
        quotas=quotas,
        split_mix=split_mix,
    )

    print("=" * 70)
    print(f"BUILD COMPLETED: Dataset Version '{effective_version}' Created Successfully")
    print("=" * 70)
    print(f"Destination: {out_dir}")
    manifest_file = out_dir / "manifest.json"
    if manifest_file.exists():
        with manifest_file.open("r", encoding="utf-8") as f:
            m = json.load(f)
        print("\n[Split Counts Summary]")
        for s_name, s_info in m.get("split_counts", {}).items():
            print(f"  - {s_name:12s}: total={s_info.get('total', 0)}")
    print("=" * 70)

    return out_dir


# ---------------------------------------------------------------------------
# ADJUDICATION V2 QUEUES EXPORT
# ---------------------------------------------------------------------------


def export_adjudication_v2(
    dataset_dir: pathlib.Path | str,
    work_dir: pathlib.Path | str,
    out_dir: Optional[pathlib.Path | str] = None,
) -> Dict[str, int]:
    """Export prioritized adjudication TSVs and CSVs for routing_v2.0.0 excluding non-English.

    Produces:
    - 1_regression.{tsv,csv} (150 rows)
    - 2_heldout.{tsv,csv}
    - 3_dev.{tsv,csv}
    in .cache/routing_eval/work/v1/adjudication_v2/ with the 18 TSV_COLUMNS.
    """
    ds_path = pathlib.Path(dataset_dir)
    work = pathlib.Path(work_dir)
    dest = pathlib.Path(out_dir) if out_dir is not None else work / "adjudication_v2"
    dest.mkdir(parents=True, exist_ok=True)

    # Load adjudication queue reasons
    queue_tsv = work / "adjudication_queue.tsv"
    queued: Dict[str, str] = {}
    if queue_tsv.exists():
        with queue_tsv.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                t_id = row.get("ticket_id", "")
                if t_id:
                    queued[t_id] = row.get("reason_queued", "manual_adjudication_request")

    def load_cases(split_name: str) -> List[RoutingCase]:
        p = ds_path / f"{split_name}.jsonl"
        if not p.exists():
            return []
        cases = read_jsonl(p)
        return [c for c in cases if c.language == "en" and is_english(c.description)]

    counts = {}

    # 1. regression (all 150 rows)
    reg_cases = load_cases("regression")
    reg_items = [
        (c, (c.failure_ref.symptom if c.failure_ref and c.failure_ref.symptom else "regression"))
        for c in reg_cases
    ]
    counts["regression"] = adjudicate.export_queue(reg_items, dest / "1_regression.tsv")

    # 2. heldout (cases in queued)
    held_cases = load_cases("heldout")
    held_items = [
        (c, queued.get(c.ticket_id, "heldout_provisional_review"))
        for c in held_cases
        if c.ticket_id in queued
    ]
    counts["heldout"] = adjudicate.export_queue(held_items, dest / "2_heldout.tsv")

    # 3. dev (cases in queued)
    dev_cases = load_cases("dev")
    dev_items = [
        (c, queued.get(c.ticket_id, "dev_provisional_review"))
        for c in dev_cases
        if c.ticket_id in queued
    ]
    counts["dev"] = adjudicate.export_queue(dev_items, dest / "3_dev.tsv")

    # Generate matching CSV files
    for tsv_file in dest.glob("*.tsv"):
        with tsv_file.open("r", encoding="utf-8", newline="") as f_in:
            reader = csv.reader(f_in, delimiter="\t")
            rows = list(reader)
        csv_file = tsv_file.with_suffix(".csv")
        with csv_file.open("w", encoding="utf-8", newline="") as f_out:
            writer = csv.writer(f_out)
            writer.writerows(rows)

    print("=" * 70)
    print("ADJUDICATION V2 QUEUES EXPORTED")
    print("=" * 70)
    print(f"Destination: {dest}")
    print(f"  - 1_regression: {counts['regression']} rows")
    print(f"  - 2_heldout:    {counts['heldout']} rows")
    print(f"  - 3_dev:        {counts['dev']} rows")
    print("=" * 70)

    return counts


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI dispatcher for routing evaluation pipeline_v1."""
    argv = list(argv if argv is not None else sys.argv[1:])

    parser = argparse.ArgumentParser(
        prog="python -m evals.routing.pipeline_v1",
        description="End-to-end data pipeline driver for routing_v1 evaluation datasets.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. stage1
    p_s1 = subparsers.add_parser("stage1", help="Collect sources and write generation/paraphrase packets.")
    p_s1.add_argument("--work", type=str, required=True, help="Working directory path.")
    p_s1.add_argument("--recipe", type=str, help="Path to recipe YAML.")
    p_s1.add_argument("--seed", type=int, help="Optional random seed override.")

    # 2. stage2
    p_s2 = subparsers.add_parser("stage2", help="Ingest generated tickets and emit annotation packets.")
    p_s2.add_argument("--work", type=str, required=True, help="Working directory path.")
    p_s2.add_argument("--generated", type=str, required=True, help="Directory containing generated responses.")
    p_s2.add_argument("--recipe", type=str, help="Path to recipe YAML.")
    p_s2.add_argument("--seed", type=int, help="Optional random seed override.")

    # 2b. stage2b
    p_s2b = subparsers.add_parser("stage2b", help="Sample fresh real tickets and export fresh annotation packets.")
    p_s2b.add_argument("--work", type=str, required=True, help="Working directory path.")
    p_s2b.add_argument("--recipe", type=str, help="Path to recipe YAML.")
    p_s2b.add_argument("--seed", type=int, help="Optional random seed override.")

    # 2c. stage2c
    p_s2c = subparsers.add_parser("stage2c", help="Sample additional fresh English tickets and export fresh2 annotation packets.")
    p_s2c.add_argument("--work", type=str, required=True, help="Working directory path.")
    p_s2c.add_argument("--recipe", type=str, help="Path to recipe YAML.")
    p_s2c.add_argument("--seed", type=int, help="Optional random seed override.")
    p_s2c.add_argument("--n", type=int, default=40, help="Number of fresh English tickets to sample.")

    # 3. stage3
    p_s3 = subparsers.add_parser("stage3", help="Ingest k-pass annotations, compute reliability and queue adjudication.")
    p_s3.add_argument("--work", type=str, required=True, help="Working directory path.")
    p_s3.add_argument("--annotations", type=str, required=True, help="Directory containing annotation responses.")
    p_s3.add_argument("--recipe", type=str, help="Path to recipe YAML.")

    # 4. build
    p_bld = subparsers.add_parser("build", help="Build final immutable dataset version directory.")
    p_bld.add_argument("--work", type=str, required=True, help="Working directory path.")
    p_bld.add_argument("--version", type=str, default=None, help="Dataset semver string (defaults to recipe dataset_version).")
    p_bld.add_argument("--adjudication", type=str, help="Optional path to human adjudication TSV.")
    p_bld.add_argument("--out-root", type=str, default="evals/data", help="Output directory root.")
    p_bld.add_argument("--recipe", type=str, help="Path to recipe YAML.")
    p_bld.add_argument("--seed", type=int, help="Optional random seed override.")
    p_bld.add_argument("--dry-run", action="store_true", help="Preview split composition without writing files.")

    # 5. adjudication_v2
    p_adj = subparsers.add_parser("adjudication_v2", help="Export adjudication v2 TSVs/CSVs.")
    p_adj.add_argument("--work", type=str, required=True, help="Working directory path.")
    p_adj.add_argument("--dataset-dir", type=str, default="evals/data/routing_v2.0.0", help="Path to routing_v2.0.0 dataset dir.")
    p_adj.add_argument("--out-dir", type=str, help="Optional output directory.")

    args = parser.parse_args(argv)

    if args.subcommand == "stage1":
        stage1(work_dir=args.work, recipe_path=args.recipe, seed=args.seed)
        return 0

    if args.subcommand == "stage2":
        stage2(work_dir=args.work, generated_dir=args.generated, recipe_path=args.recipe, seed=args.seed)
        return 0

    if args.subcommand == "stage2b":
        stage2b(work_dir=args.work, recipe_path=args.recipe, seed=args.seed)
        return 0

    if args.subcommand == "stage2c":
        stage2c(work_dir=args.work, recipe_path=args.recipe, seed=args.seed, n_sample=args.n)
        return 0

    if args.subcommand == "stage3":
        stage3(work_dir=args.work, annotations_dir=args.annotations, recipe_path=args.recipe)
        return 0

    if args.subcommand == "build":
        build(
            work_dir=args.work,
            version=args.version,
            adjudication_tsv=args.adjudication,
            out_root=args.out_root,
            recipe_path=args.recipe,
            seed=args.seed,
            dry_run=args.dry_run,
        )
        return 0

    if args.subcommand == "adjudication_v2":
        export_adjudication_v2(dataset_dir=args.dataset_dir, work_dir=args.work, out_dir=args.out_dir)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
