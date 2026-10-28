"""CLI commands for routing evaluation data operations.

# REQ-REVAL-004 / REQ-REVAL-006 / REQ-REVAL-007 / REQ-REVAL-020: Data CLI
Provides:
cli_data.main(argv) with subcommands:
  - sources
  - export-annotations
  - ingest-annotations
  - export-generation
  - ingest-generation
  - export-adjudication
  - ingest-adjudication
  - add-regression
  - build
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import List, Optional

import yaml

from evals.routing import (
    adjudicate,
    annotate,
    build,
    regression,
)
from evals.routing.schema import read_jsonl, write_jsonl
from evals.routing.sources import public_bitext, synthetic


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint for routing data operations."""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="python -m evals.routing data",
        description="Dataset pipeline tools for routing evaluations.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. sources
    sub_sources = subparsers.add_parser("sources", help="Emit dataset pool candidates.")
    sub_sources.add_argument("--bitext-csv", type=str, help="Path to raw Bitext CSV.")
    sub_sources.add_argument("--n", type=int, default=100, help="Number of rows to sample.")
    sub_sources.add_argument("--seed", type=int, default=20261028, help="Random seed.")
    sub_sources.add_argument("--out", type=str, required=True, help="Path to output JSONL.")

    # 2. export-annotations
    sub_exp_ann = subparsers.add_parser("export-annotations", help="Export k-pass annotation packets.")
    sub_exp_ann.add_argument("--cases", type=str, required=True, help="Path to input cases JSONL.")
    sub_exp_ann.add_argument("--k", type=int, default=3, help="Passes count.")
    sub_exp_ann.add_argument("--seed", type=int, default=20261028, help="Random seed.")
    sub_exp_ann.add_argument("--out", type=str, required=True, help="Path to output packets JSONL.")

    # 3. ingest-annotations
    sub_ing_ann = subparsers.add_parser("ingest-annotations", help="Ingest k-pass annotation responses.")
    sub_ing_ann.add_argument("--responses", type=str, required=True, help="Path to responses JSONL.")
    sub_ing_ann.add_argument("--cases", type=str, required=True, help="Path to input cases JSONL.")
    sub_ing_ann.add_argument("--out", type=str, required=True, help="Path to output updated cases JSONL.")

    # 4. export-generation
    sub_exp_gen = subparsers.add_parser("export-generation", help="Export generation packets for offline synthesis.")
    sub_exp_gen.add_argument("--spec", type=str, help="Path to spec cells JSON.")
    sub_exp_gen.add_argument("--seed", type=int, default=20261028, help="Random seed.")
    sub_exp_gen.add_argument("--out", type=str, required=True, help="Path to output packets JSONL.")

    # 5. ingest-generation
    sub_ing_gen = subparsers.add_parser("ingest-generation", help="Ingest synthetic generated cases.")
    sub_ing_gen.add_argument("--input", type=str, required=True, help="Path to generated JSONL.")
    sub_ing_gen.add_argument("--out", type=str, required=True, help="Path to output cases JSONL.")

    # 6. export-adjudication
    sub_exp_adj = subparsers.add_parser("export-adjudication", help="Export queued cases to TSV for adjudication.")
    sub_exp_adj.add_argument("--cases", type=str, required=True, help="Path to cases JSONL.")
    sub_exp_adj.add_argument("--out", type=str, required=True, help="Path to output TSV.")

    # 7. ingest-adjudication
    sub_ing_adj = subparsers.add_parser("ingest-adjudication", help="Ingest decisions from adjudication TSV.")
    sub_ing_adj.add_argument("--tsv", type=str, required=True, help="Path to decisions TSV.")
    sub_ing_adj.add_argument("--cases", type=str, required=True, help="Path to cases JSONL.")
    sub_ing_adj.add_argument("--out", type=str, required=True, help="Path to output cases JSONL.")
    sub_ing_adj.add_argument("--supersede", action="store_true", help="Allow overwriting conflicting decisions.")

    # 8. add-regression
    sub_add_reg = subparsers.add_parser("add-regression", help="Append a newly-found misroute to regression set.")
    sub_add_reg.add_argument("--text", type=str, required=True, help="Ticket message text.")
    sub_add_reg.add_argument("--symptom", type=str, required=True, help="Observed symptom.")
    sub_add_reg.add_argument("--root-cause", type=str, required=True, help="Identified root cause.")
    sub_add_reg.add_argument("--intent", type=str, required=True, help="Canonical expected intent.")
    sub_add_reg.add_argument("--decision", type=str, default="auto_rag", choices=["auto_rag", "escalate"])
    sub_add_reg.add_argument("--queue", type=str, default="none", choices=["none", "human-standard", "human-urgent", "human-low"])
    sub_add_reg.add_argument("--sentiment", type=str, default="neutral")
    sub_add_reg.add_argument("--urgency", type=int, default=1)
    sub_add_reg.add_argument("--seeds-path", type=str, help="Path to regression_seeds.yaml.")

    # 9. build
    sub_build = subparsers.add_parser("build", help="Build immutable dataset version.")
    sub_build.add_argument("--version", type=str, required=True, help="Dataset version (e.g. routing_v1.0.0).")
    sub_build.add_argument("--config", type=str, required=True, help="Path to inputs config YAML/JSON.")
    sub_build.add_argument("--seed", type=int, default=20261028, help="Random seed.")
    sub_build.add_argument("--out-root", type=str, default="evals/data", help="Output directory root.")

    args = parser.parse_args(argv)

    if args.subcommand == "sources":
        if args.bitext_csv:
            cases = public_bitext.sample(args.bitext_csv, n=args.n, seed=args.seed)
        else:
            cases = synthetic.generate_templates(seed=args.seed, n_per_cell=max(1, args.n // 10))
        write_jsonl(args.out, cases)
        print(f"Wrote {len(cases)} cases to {args.out}")
        return 0

    if args.subcommand == "export-annotations":
        cases = read_jsonl(args.cases)
        packets = annotate.export_packets(cases, k=args.k, seed=args.seed, out_path=args.out)
        print(f"Exported {len(packets)} annotation packets to {args.out}")
        return 0

    if args.subcommand == "ingest-annotations":
        cases = read_jsonl(args.cases)
        updated, report = annotate.ingest(args.responses, cases)
        write_jsonl(args.out, updated)
        print(f"Ingested annotations: {report['ingested_cases_count']} cases updated; wrote to {args.out}")
        return 0

    if args.subcommand == "export-generation":
        if args.spec and pathlib.Path(args.spec).exists():
            with open(args.spec, "r", encoding="utf-8") as f:
                cells = json.load(f)
        else:
            cells = synthetic.default_cells()
        packets = synthetic.export_generation_packets(cells, seed=args.seed, out_path=args.out)
        print(f"Exported {len(packets)} generation packets to {args.out}")
        return 0

    if args.subcommand == "ingest-generation":
        cases, report = synthetic.ingest_generated(args.input)
        write_jsonl(args.out, cases)
        print(f"Ingested {len(cases)} synthetic cases (rejected {report['rejected_count']}); wrote to {args.out}")
        return 0

    if args.subcommand == "export-adjudication":
        cases = read_jsonl(args.cases)
        queued = annotate.queue_for_adjudication(cases)
        count = adjudicate.export_queue(queued if queued else cases, args.out)
        print(f"Exported {count} cases for adjudication to {args.out}")
        return 0

    if args.subcommand == "ingest-adjudication":
        cases = read_jsonl(args.cases)
        updated, report = adjudicate.ingest_decisions(args.tsv, cases, supersede=args.supersede)
        write_jsonl(args.out, updated)
        print(f"Ingested adjudication: {report['processed_count']} processed; wrote to {args.out}")
        return 0

    if args.subcommand == "add-regression":
        exp = {
            "intent": args.intent,
            "acceptable_intents": [args.intent],
            "escalate": (args.decision == "escalate"),
            "decision": args.decision,
            "queue": args.queue,
            "sentiment": args.sentiment,
            "urgency": args.urgency,
        }
        case = regression.add_regression(
            text=args.text,
            symptom=args.symptom,
            root_cause=args.root_cause,
            expected=exp,
            seeds_path=args.seeds_path or regression._DEFAULT_SEEDS_PATH,
        )
        print(f"Appended regression case {case.failure_ref.failure_id if case.failure_ref else 'REG'} with ticket_id {case.ticket_id}")
        return 0

    if args.subcommand == "build":
        p = pathlib.Path(args.config)
        with p.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) if p.suffix in {".yaml", ".yml"} else json.load(f)
        out_path = build.build_version(
            version=args.version,
            inputs_cfg=cfg,
            seed=args.seed,
            out_root=args.out_root,
        )
        print(f"Successfully built dataset version {args.version} at {out_path}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
