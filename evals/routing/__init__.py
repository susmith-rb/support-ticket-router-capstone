"""Independent Routing-System Evaluation Harness.

Work Package A: Data core, taxonomy, schemas, provenance, dedup, splits,
manifest, and heldout guard.
"""

from evals.routing.dedup import (
    check_leakage,
    cluster_near_duplicates,
    deduplicate_and_cluster,
    exact_deduplicate,
)
from evals.routing.heldout import (
    check_allowed,
    preregister,
    record_result,
)
from evals.routing.manifest import (
    build_manifest,
    compute_split_breakdown,
    write_version_dir,
)
from evals.routing.provenance import (
    PROVENANCE_STRENGTH,
    VALID_GROUND_TRUTH_SOURCES,
    derive_status,
    update_provenance,
)
from evals.routing.schema import (
    SCHEMA_VERSION,
    VALID_CHANNELS,
    VALID_DIFFICULTIES,
    VALID_LABEL_STATUSES,
    VALID_REGISTERS,
    VALID_SCENARIOS,
    VALID_SOURCE_DATASETS,
    VALID_SOURCES,
    VALID_SPLITS,
    Adjudication,
    Annotation,
    Expected,
    FailureRef,
    Perturbation,
    RoutingCase,
    Style,
    compute_content_hash,
    make_ticket_id,
    normalize_text,
    read_jsonl,
    validate_case,
    write_jsonl,
)
from evals.routing.split import (
    DEFAULT_QUOTAS,
    assign,
)

__all__ = [
    "DEFAULT_QUOTAS",
    "PROVENANCE_STRENGTH",
    "SCHEMA_VERSION",
    "VALID_CHANNELS",
    "VALID_DIFFICULTIES",
    "VALID_GROUND_TRUTH_SOURCES",
    "VALID_LABEL_STATUSES",
    "VALID_REGISTERS",
    "VALID_SCENARIOS",
    "VALID_SOURCES",
    "VALID_SOURCE_DATASETS",
    "VALID_SPLITS",
    "Adjudication",
    "Annotation",
    "Expected",
    "FailureRef",
    "Perturbation",
    "RoutingCase",
    "Style",
    "assign",
    "build_manifest",
    "check_allowed",
    "check_leakage",
    "cluster_near_duplicates",
    "compute_content_hash",
    "compute_split_breakdown",
    "deduplicate_and_cluster",
    "derive_status",
    "exact_deduplicate",
    "make_ticket_id",
    "normalize_text",
    "preregister",
    "read_jsonl",
    "record_result",
    "update_provenance",
    "validate_case",
    "write_jsonl",
    "write_version_dir",
]
