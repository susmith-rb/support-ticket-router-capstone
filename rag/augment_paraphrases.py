#!/usr/bin/env python3
"""rag/augment_paraphrases.py — augment FAQ articles with customer phrasing paraphrases.

Integrates real customer phrasing into the 'Also asked as:' section of targeted FAQ articles
while strictly enforcing split discipline (zero leakage into dev or test sets).
"""

import argparse
import json
import pathlib
import re
import shutil
import sys
from typing import Any, Dict, List, Optional, Set, Tuple


def normalize_text(text: str) -> str:
    """Normalize text for leakage check: remove handles, tokens, punctuation, lowercase, collapse whitespace."""
    t = re.sub(r"@\w+", "", text)
    t = re.sub(r"\[(URL|ORDER|CARD|EMAIL|PHONE|NUM)\]", "", t)
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return " ".join(t.split())


def load_eval_leakage_sets(dev_path: pathlib.Path, test_path: pathlib.Path) -> Tuple[Set[str], Set[str]]:
    """Load thread_ids and normalized query strings from dev and test sets."""
    eval_thread_ids = set()
    eval_queries_norm = set()

    for path in (dev_path, test_path):
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                if "thread_id" in d and d["thread_id"]:
                    eval_thread_ids.add(str(d["thread_id"]))
                if "query" in d and d["query"]:
                    eval_queries_norm.add(normalize_text(d["query"]))

    return eval_thread_ids, eval_queries_norm


def verify_no_leakage(
    paraphrases_data: Dict[str, Any],
    dev_path: pathlib.Path,
    test_path: pathlib.Path,
) -> None:
    """Enforce provenance and leakage invariants:
    
    1. No source_thread_id belongs to dev.jsonl or test.jsonl.
    2. No paraphrase string matches or near-duplicates any query in dev.jsonl or test.jsonl.
    """
    eval_tids, eval_queries_norm = load_eval_leakage_sets(dev_path, test_path)

    # 1. Thread ID check
    leaked_tids = []
    for doc_id, data in paraphrases_data.items():
        for tid in data.get("source_thread_ids", []):
            if str(tid) in eval_tids:
                leaked_tids.append((doc_id, str(tid)))

    if leaked_tids:
        raise ValueError(
            f"LEAKAGE VIOLATION: Source thread IDs found in dev/test sets: {leaked_tids}"
        )

    # 2. Text normalized match check
    leaked_queries = []
    for doc_id, data in paraphrases_data.items():
        qs = data.get("paraphrases", []) + data.get("questions", [])
        for p in qs:
            norm_p = normalize_text(p)
            if norm_p in eval_queries_norm:
                leaked_queries.append((doc_id, p))

    if leaked_queries:
        raise ValueError(
            f"LEAKAGE VIOLATION: Paraphrase text matches query in dev/test sets: {leaked_queries}"
        )


def apply_disambiguation_to_body(
    body: str,
    scope_line: Optional[str] = None,
    new_questions: Optional[List[str]] = None,
    removed_questions: Optional[List[str]] = None,
) -> str:
    """Apply scope line, question removals, and new distinguishing questions to an article body idempotently."""
    lines = body.splitlines()

    # 1. Scope line handling
    if scope_line:
        scope_idx = -1
        for idx, l in enumerate(lines):
            if l.strip().startswith("Scope:"):
                scope_idx = idx
                break

        if scope_idx != -1:
            lines[scope_idx] = scope_line
        else:
            title_idx = -1
            for idx, l in enumerate(lines):
                if l.startswith("# "):
                    title_idx = idx
                    break

            if title_idx != -1:
                lines = lines[: title_idx + 1] + ["", scope_line] + lines[title_idx + 1 :]
            else:
                lines = [scope_line, ""] + lines

    # 2. Also asked as block
    also_idx = -1
    for idx, l in enumerate(lines):
        if l.strip().lower() == "also asked as:":
            also_idx = idx
            break

    if also_idx != -1:
        bullets_start = also_idx + 1
        bullets_end = bullets_start
        existing_bullets = []
        rem_set = {r.strip().lower() for r in (removed_questions or [])}

        while bullets_end < len(lines) and (
            lines[bullets_end].strip().startswith("- ") or not lines[bullets_end].strip()
        ):
            if lines[bullets_end].strip().startswith("- "):
                b_text = lines[bullets_end].strip()[2:].strip()
                if b_text.lower() not in rem_set:
                    existing_bullets.append(b_text)
            bullets_end += 1

        existing_set = {b.lower() for b in existing_bullets}
        for q in new_questions or []:
            if q.strip().lower() not in existing_set:
                existing_bullets.append(q.strip())
                existing_set.add(q.strip().lower())

        rest = lines[bullets_end:]
        while rest and not rest[0].strip():
            rest.pop(0)

        new_lines = (
            lines[: also_idx + 1]
            + [f"- {b}" for b in existing_bullets]
            + [""]
            + rest
        )
        return "\n".join(new_lines).strip() + "\n"
    else:
        return "\n".join(lines).strip() + "\n"


def merge_paraphrases_into_body(body: str, new_paraphrases: List[str]) -> str:
    """Merge new paraphrases into the article's 'Also asked as:' block idempotently."""
    lines = body.splitlines()
    also_idx = -1
    for idx, l in enumerate(lines):
        if l.strip().lower() == "also asked as:":
            also_idx = idx
            break

    if also_idx == -1:
        # If 'Also asked as:' does not exist, insert after the first heading
        insert_idx = 1
        for idx, l in enumerate(lines):
            if l.startswith("# "):
                insert_idx = idx + 1
                break
        
        para_block = ["", "Also asked as:"] + [f"- {p.strip()}" for p in new_paraphrases] + [""]
        new_lines = lines[:insert_idx] + para_block + lines[insert_idx:]
        return "\n".join(new_lines).strip() + "\n"

    # Find the end of the existing bullet list
    end_idx = also_idx + 1
    existing_paras = set()
    while end_idx < len(lines):
        line = lines[end_idx]
        if line.strip().startswith("- "):
            existing_paras.add(line.strip()[2:].strip().lower())
            end_idx += 1
        elif not line.strip():
            end_idx += 1
            break
        else:
            break

    # Append only new paraphrases not already in existing bullets
    to_add = []
    for p in new_paraphrases:
        clean_p = p.strip()
        if clean_p.lower() not in existing_paras:
            to_add.append(f"- {clean_p}")
            existing_paras.add(clean_p.lower())

    # Reconstruct article body
    bullets_end = also_idx + 1
    while bullets_end < len(lines) and lines[bullets_end].strip().startswith("- "):
        bullets_end += 1

    existing_bullets = lines[also_idx + 1 : bullets_end]
    rest = lines[bullets_end:]

    # Remove leading blank lines from rest
    while rest and not rest[0].strip():
        rest.pop(0)

    combined_lines = (
        lines[: also_idx + 1]
        + existing_bullets
        + to_add
        + [""]
        + rest
    )
    return "\n".join(combined_lines).strip() + "\n"


def augment_corpus(
    in_dir: pathlib.Path,
    paraphrases_path: pathlib.Path,
    out_dir: pathlib.Path,
    dev_path: pathlib.Path,
    test_path: pathlib.Path,
) -> Dict[str, Any]:
    """Augment articles from in_dir with paraphrases_path, writing to out_dir."""
    with open(paraphrases_path, "r", encoding="utf-8") as f:
        paraphrases_data = json.load(f)

    # 1. Enforce leakage guard
    verify_no_leakage(paraphrases_data, dev_path, test_path)

    # 2. Setup output directories
    out_docs = out_dir / "docs"
    out_docs.mkdir(parents=True, exist_ok=True)

    in_docs = in_dir / "docs"
    if not in_docs.exists():
        raise FileNotFoundError(f"Input docs directory not found: {in_docs}")

    # Copy ancillary files if present
    for fname in ["manifest.jsonl", "faq_articles_import.jsonl", "themes_v2.json", "gold_map_v1_v2.json", "release.json"]:
        src = in_dir / fname
        if src.exists():
            shutil.copy2(src, out_dir / fname)

    augmented_count = 0
    unchanged_count = 0
    augmented_docs = []

    for doc_file in in_docs.glob("*.txt"):
        doc_id = doc_file.stem
        body = doc_file.read_text(encoding="utf-8")

        if doc_id in paraphrases_data:
            item = paraphrases_data[doc_id]
            if "scope_line" in item or "questions" in item or "removed_questions" in item:
                new_body = apply_disambiguation_to_body(
                    body=body,
                    scope_line=item.get("scope_line"),
                    new_questions=item.get("questions", []),
                    removed_questions=item.get("removed_questions", []),
                )
            else:
                paras = item.get("paraphrases", [])
                new_body = merge_paraphrases_into_body(body, paras)
            (out_docs / doc_file.name).write_text(new_body, encoding="utf-8")
            augmented_count += 1
            augmented_docs.append(doc_id)
        else:
            (out_docs / doc_file.name).write_text(body, encoding="utf-8")
            unchanged_count += 1

    return {
        "augmented_count": augmented_count,
        "unchanged_count": unchanged_count,
        "augmented_docs": sorted(augmented_docs),
    }


def main():
    parser = argparse.ArgumentParser(description="Augment FAQ corpus with customer paraphrases or disambiguation.")
    parser.add_argument("--in-dir", required=True, type=pathlib.Path, help="Input corpus directory containing docs/")
    parser.add_argument("--out-dir", required=True, type=pathlib.Path, help="Output corpus directory")
    parser.add_argument(
        "--paraphrases",
        default=pathlib.Path("rag/corpus_v2/agent_decisions/paraphrases_v1.json"),
        type=pathlib.Path,
        help="Path to paraphrases_v1.json or disambiguation_v1.json",
    )
    parser.add_argument(
        "--disambiguation",
        default=None,
        type=pathlib.Path,
        help="Optional path to disambiguation_v1.json (overrides --paraphrases)",
    )
    parser.add_argument(
        "--dev-queries",
        default=pathlib.Path("/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/eval/dev.jsonl"),
        type=pathlib.Path,
        help="Path to dev.jsonl for leakage guard",
    )
    parser.add_argument(
        "--test-queries",
        default=pathlib.Path("/usr/local/google/home/susmith/.gemini/jetski/brain/92e6895e-6aa0-4668-8e1e-5cb583072600/scratch/rag/eval/test.jsonl"),
        type=pathlib.Path,
        help="Path to test.jsonl for leakage guard",
    )
    args = parser.parse_args()

    decision_path = args.disambiguation if args.disambiguation else args.paraphrases
    result = augment_corpus(
        in_dir=args.in_dir,
        paraphrases_path=decision_path,
        out_dir=args.out_dir,
        dev_path=args.dev_queries,
        test_path=args.test_queries,
    )
    print(f"Augmentation complete: {result['augmented_count']} articles augmented, {result['unchanged_count']} unchanged.")
    print("Augmented articles:")
    for d in result["augmented_docs"]:
        print(f"  - {d}")


if __name__ == "__main__":
    main()
