# Specification: Platform Architecture & Packaging (`platform`)

This specification defines the repository package structure, dependency isolation between runtime services and offline tooling, runtime authentication contracts, and import cleanliness invariants across the project.

---

## 1. Overview & Architectural Principles

The repository supports both production runtime services (agent services, model endpoints, and retrieval clients) and offline engineering tooling (evaluation gates, dataset curation, corpus publishing, and model training).

### Key Architectural Invariants

1. **Standard Python Packaging**: The repository is packaged via `pyproject.toml` using `setuptools`. Runtime code lives under `src/ticket_router/` with modular namespaces (`config`, `common`, `retrieval`). Offline tools live in structured top-level packages (`evals`, `corpus`, `experiments`, `training`).
2. **Zero `sys.path` Mutations**: No production code, scripts, or test suites may manipulate `sys.path` via `sys.path.insert` or `sys.path.append`. All code relies on standard Python package resolution.
3. **Runtime Auth via Google Auth ADC**: Production libraries obtain GCP credentials and OAuth tokens exclusively via `google.auth` Application Default Credentials (ADC) with in-process token caching. Spawning `gcloud auth print-access-token` in child processes is strictly forbidden in runtime code.
4. **Offline vs. Runtime Isolation**: Production runtime code under `src/ticket_router/` must remain completely independent of offline packages (`evals`, `corpus`, `experiments`, `training`).

---

## 2. Requirement Index

| ID | Title | Status | Acceptance |
|---|---|---|---|
| `REQ-PLAT-001` | Standard Installable Package Layout and Zero Sys.path Hacks | `done` | `tests/test_platform.py::test_zero_sys_path_mutations` |
| `REQ-PLAT-002` | Runtime Authentication via Google Auth ADC with Token Caching | `done` | `tests/test_platform.py::test_runtime_auth_adc_token_caching` |
| `REQ-PLAT-003` | Strict Runtime vs. Offline Dependency Isolation | `done` | `tests/test_platform.py::test_runtime_offline_isolation` |

---

## 3. Detailed Requirements

### REQ-PLAT-001: Standard Installable Package Layout and Zero Sys.path Hacks
- **Statement**: The repository MUST structure runtime and offline code into standard Python packages (`ticket_router` under `src/`, with top-level `evals`, `corpus`, `experiments`, `training` packages configured in `pyproject.toml`). All modules and tests MUST use standard absolute package imports. Direct runtime path hacks (`sys.path.insert`, `sys.path.append`) are strictly forbidden across `src/`, `evals/`, `corpus/`, `experiments/`, and `tests/`.
- **Rationale**: Ensures clean packaging for local development, CI/CD, and production container builds while eliminating fragile directory-dependent import side effects (Phase-0 RESTRUCTURE).
- **Acceptance**: `tests/test_platform.py::test_zero_sys_path_mutations`
- **Status**: done
- **Owner**: platform

### REQ-PLAT-002: Runtime Authentication via Google Auth ADC with Token Caching
- **Statement**: Runtime components under `src/ticket_router/` MUST obtain Google Cloud credentials and OAuth tokens exclusively via `ticket_router.common.auth` using `google.auth.default()`, caching credentials and valid tokens in-memory. Subprocess execution of the `gcloud` CLI (e.g. `gcloud auth print-access-token`) is strictly prohibited across runtime code and offline packages (`src/`, `evals/`, `corpus/`, `experiments/`).
- **Rationale**: Prevents high latency and process spawning overhead on every API call, enables hermetic credential mocking in unit tests, and complies with standard Cloud Run/Workload Identity execution environments.
- **Acceptance**: `tests/test_platform.py::test_runtime_auth_adc_token_caching` and `tests/test_platform.py::test_zero_print_access_token_shelling`
- **Status**: done
- **Owner**: platform

### REQ-PLAT-003: Strict Runtime vs. Offline Dependency Isolation
- **Statement**: Runtime code under `src/ticket_router/` MUST NOT import from offline packages (`evals`, `corpus`, `experiments`, or `training`). Offline modules may import from `src/ticket_router`, but runtime services and libraries must remain self-contained.
- **Rationale**: Prevents deployment container bloat, avoids pulling offline evaluation/training dependencies into production runtimes, and enforces clear architectural boundaries.
- **Acceptance**: `tests/test_platform.py::test_runtime_offline_isolation`
- **Status**: done
- **Owner**: platform
