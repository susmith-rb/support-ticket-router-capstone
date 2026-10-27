"""Guard test ensuring no caller-asserted identity flows into customer_id.

# REQ-IDENT-007: Identity Threat Mitigations (Anti-Spoofing & Derived RLS)
# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution
"""

from __future__ import annotations

import re
from pathlib import Path


def test_no_caller_asserted_identity_flows_into_customer_id():
    """Enforce architectural invariant: caller-asserted user_id never flows into customer_id.

    Scans src/ticket_router/**/*.py:
    1. Fails if any function is named 'extract_customer_id' (legacy spoofing vector).
    2. Fails if any line contains both 'user_id' and 'customer_id' (e.g. assigning or mapping).
    """
    root_dir = Path(__file__).resolve().parents[2] / "src" / "ticket_router"
    assert root_dir.is_dir(), f"Directory not found: {root_dir}"

    py_files = sorted(root_dir.glob("**/*.py"))
    assert py_files, "No python files found to scan"

    violations: list[str] = []

    for file_path in py_files:
        rel_path = file_path.relative_to(root_dir)
        content = file_path.read_text(encoding="utf-8")

        # Check 1: No function named extract_customer_id
        if re.search(r"\bdef\s+extract_customer_id\b", content):
            violations.append(f"{rel_path}: Contains forbidden function 'extract_customer_id'")

        # Check 2: No line containing both 'user_id' and 'customer_id'
        for line_no, line in enumerate(content.splitlines(), start=1):
            if "user_id" in line and "customer_id" in line:
                violations.append(
                    f"{rel_path}:{line_no}: Line contains both 'user_id' and 'customer_id': {line.strip()}"
                )

    assert not violations, "Violations detected:\n" + "\n".join(violations)
