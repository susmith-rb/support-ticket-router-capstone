"""Data source adapters for routing evaluation cases.

# REQ-REVAL-004: Source adapters (real, public, synthetic)
"""

from evals.routing.sources.public_bitext import (
    load_mapping,
)
from evals.routing.sources.public_bitext import (
    sample as sample_bitext,
)
from evals.routing.sources.real_twcs import (
    contamination_guard,
    load_golden_consensus,
    load_retrieval_c2,
    sample_inbound_parquet,
    scrub_text,
)
from evals.routing.sources.synthetic import (
    default_cells,
    export_generation_packets,
    generate_templates,
    ingest_generated,
)

__all__ = [
    "contamination_guard",
    "default_cells",
    "export_generation_packets",
    "generate_templates",
    "ingest_generated",
    "load_golden_consensus",
    "load_mapping",
    "load_retrieval_c2",
    "sample_bitext",
    "sample_inbound_parquet",
    "scrub_text",
]
