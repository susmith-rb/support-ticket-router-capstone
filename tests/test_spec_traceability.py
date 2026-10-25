"""Tests for spec-driven development traceability.

Validates that:
1. All requirement IDs in specs/*.md are unique and well-formed.
2. No duplicate REQ headings exist within or across specification files.
3. Every requirement defines statement, rationale, acceptance, status, and owner.
4. Every 'done' requirement has at least one test reference (# REQ-...) in tests/**
   OR an acceptance command explicitly marked 'manual:'.
5. Every requirement ID referenced in tests exists in the specs.
"""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = REPO_ROOT / "specs"
TESTS_DIR = REPO_ROOT / "tests"

REQ_ID_PATTERN = re.compile(r"^REQ-[A-Z0-9]+(-[A-Z0-9]+)*-\d{3}$")
REQ_HEADER_PATTERN = re.compile(r"^###\s+(REQ-[A-Z0-9]+(-[A-Z0-9]+)*-\d{3})(?::\s*(.*))?$")
TEST_REQ_REF_PATTERN = re.compile(r"#\s*(REQ-[A-Z0-9]+(-[A-Z0-9]+)*-\d{3})")
VALID_STATUSES = {"done", "partial", "open", "deferred"}


def parse_requirements():
  """Parses all requirement blocks from specs/*.md files (excluding README.md)."""
  specs = {}
  spec_files = sorted(SPECS_DIR.glob("*.md"))
  for spec_file in spec_files:
    if spec_file.name == "README.md":
      continue
    text = spec_file.read_text(encoding="utf-8")
    current_req = None
    for line in text.splitlines():
      header_match = REQ_HEADER_PATTERN.match(line)
      if header_match:
        req_id = header_match.group(1)
        title = header_match.group(3) or ""
        current_req = {
            "id": req_id,
            "title": title.strip(),
            "file": spec_file.name,
            "statement": None,
            "rationale": None,
            "acceptance": None,
            "status": None,
            "owner": None,
        }
        specs[req_id] = current_req
        continue

      if current_req:
        stripped = line.strip()
        if stripped.startswith("- **Statement**:"):
          current_req["statement"] = stripped.split("- **Statement**:", 1)[1].strip()
        elif stripped.startswith("- **Rationale**:"):
          current_req["rationale"] = stripped.split("- **Rationale**:", 1)[1].strip()
        elif stripped.startswith("- **Acceptance**:"):
          val = stripped.split("- **Acceptance**:", 1)[1].strip()
          if val.startswith("`") and val.endswith("`"):
            val = val[1:-1].strip()
          current_req["acceptance"] = val
        elif stripped.startswith("- **Status**:"):
          current_req["status"] = stripped.split("- **Status**:", 1)[1].strip().lower()
        elif stripped.startswith("- **Owner**:"):
          current_req["owner"] = stripped.split("- **Owner**:", 1)[1].strip()
        elif line.startswith("## ") or (line.startswith("### ") and not REQ_HEADER_PATTERN.match(line)):
          current_req = None

  return specs


def get_test_req_references():
  """Collects all REQ references found in comments across tests/**."""
  references = {}
  for test_file in sorted(TESTS_DIR.glob("**/*.py")):
    if test_file.name == "test_spec_traceability.py":
      continue
    lines = test_file.read_text(encoding="utf-8").splitlines()
    for line_no, line in enumerate(lines, 1):
      for match in TEST_REQ_REF_PATTERN.finditer(line):
        req_id = match.group(1)
        references.setdefault(req_id, []).append((str(test_file.relative_to(REPO_ROOT)), line_no))
  return references


def test_req_ids_well_formed_and_unique():
  """All requirement IDs in specs must be unique and match standard format."""
  seen_ids = {}
  spec_files = sorted(SPECS_DIR.glob("*.md"))
  assert spec_files, "No spec files found in specs/"

  duplicates = []
  for spec_file in spec_files:
    if spec_file.name == "README.md":
      continue
    for line_no, line in enumerate(spec_file.read_text(encoding="utf-8").splitlines(), 1):
      m = REQ_HEADER_PATTERN.match(line)
      if m:
        req_id = m.group(1)
        assert REQ_ID_PATTERN.match(req_id), f"Malformed REQ ID: '{req_id}' in {spec_file.name}:{line_no}"
        if req_id in seen_ids:
          duplicates.append(
              f"Duplicate REQ heading '{req_id}' in {spec_file.name}:{line_no} "
              f"(previously defined in {seen_ids[req_id]})"
          )
        else:
          seen_ids[req_id] = f"{spec_file.name}:{line_no}"

  assert not duplicates, "Found duplicate REQ headings:\n" + "\n".join(duplicates)


def test_requirements_have_valid_fields():
  """Every requirement must specify valid statement, rationale, acceptance, status, and owner."""
  specs = parse_requirements()
  assert specs, "No requirements found in specs/*.md"
  for req_id, req in specs.items():
    assert req["statement"], f"Missing statement in {req_id} ({req['file']})"
    assert req["rationale"], f"Missing rationale in {req_id} ({req['file']})"
    assert req["acceptance"], f"Missing acceptance in {req_id} ({req['file']})"
    assert req["status"] in VALID_STATUSES, (
        f"Invalid status '{req['status']}' in {req_id} ({req['file']}); must be one of {VALID_STATUSES}"
    )
    assert req["owner"], f"Missing owner in {req_id} ({req['file']})"


def test_done_requirements_have_traceability():
  """Every 'done' requirement must have at least one test reference in tests/** OR a manual acceptance command."""
  specs = parse_requirements()
  test_refs = get_test_req_references()

  untraced = []
  for req_id, req in specs.items():
    if req["status"] == "done":
      is_in_tests = req_id in test_refs and len(test_refs[req_id]) > 0
      is_manual = req["acceptance"].lower().startswith("manual:")
      if not (is_in_tests or is_manual):
        untraced.append(f"{req_id} in {req['file']} (acceptance: {req['acceptance']})")

  assert not untraced, "Done requirements without test reference or manual acceptance command:\n" + "\n".join(untraced)


def test_all_test_references_exist_in_specs():
  """Every REQ ID referenced in test files must exist in the specs."""
  specs = parse_requirements()
  test_refs = get_test_req_references()

  unknown = []
  for req_id, locations in test_refs.items():
    if req_id not in specs:
      loc_str = ", ".join(f"{f}:{l}" for f, l in locations)
      unknown.append(f"{req_id} (referenced at {loc_str})")

  assert not unknown, "Test files reference unknown requirement IDs:\n" + "\n".join(unknown)
