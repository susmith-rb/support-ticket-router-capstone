# Lessons log

Mistakes by the agent, the harness or the environment, and what changed because of them. Rules
that come out of these entries go into [AGENTS.md](../AGENTS.md).

| # | Date | What happened | Fix / rule |
| --- | --- | --- | --- |
| L1 | 2026-10-21 | `laya-train` was "command not found": PyPI `laya` 0.3.20 (the Model Garden serving version) ships no trainer; it exists only in 0.4.x | Two venvs: `.venv` (0.3.20, eval/serve) and `.venv-train` (0.4.13, train). `train.sh` evaluates under 0.3.20 to prove servability |
| L2 | 2026-10-21 | `huggingface-cli` not found; the HF repo root is the `english` checkpoint, with `typed-decisions/` and `multilingual/` as subfolders; `LocalLLaMA/typed-decisions` needs a config name (`all`, `customer_service`, …) | Use `huggingface_hub.snapshot_download` from Python; always pass a dataset config |
| L3 | 2026-10-21 | Workbench system Python is PEP 668-locked, so `pip install` failed | Create venvs from `/opt/micromamba/bin/python` |
| L4 | 2026-10-21 | VM creation failed four times on Argolis org policies: no default VPC, external IPs denied, Shielded VM + Secure Boot required, then a T4 zone stockout | Custom VPC + Cloud NAT + `--disable-public-ip` + shielded flags; try zones in order. Documented in the training runbook |
| L5 | 2026-10-21 | Agent's zone-retry script treated "created but still PROVISIONING" as failure and **deleted two successfully created VMs** | Success check must accept PROVISIONING/RUNNING and never delete on an ambiguous state |
| L6 | 2026-10-21 | Redaction turned Amazon order numbers (`112-1234567-1234567`) into `[CARD]` because the card regex ran first | Order-ID regex runs before cards; cards must pass Luhn. Covered by `test_redact.py` |
| L7 | 2026-10-21 | `gemini-3.1-pro` returned 404 on Vertex in this project | Teacher uses `gemini-3.1-pro-preview` at `global`; the labeler probes model and location before a long run |
| L8 | 2026-10-21 | OS Login for the `gcp.altostrat.com` identity on an in-org VM needs `roles/compute.osLoginExternalUser` at org level, and the grant propagates slowly | SSH with the in-org `admin@` identity |
| L9 | 2026-10-21 | `scp` over the IAP tunnel failed; the VM's default SA had no bucket access | Move files through the GCS bucket; grant the VM SA `objectAdmin` on the bucket |
| L10 | 2026-10-21 | Agent quoted teacher-labeling cost as \$5–10 (v1) and \$20–40 (v2); measured usage showed ~345 thinking tokens per call (85% of billed output), making the real cost ≈ 4–5× higher (\$0.017 per message at 3 samples) | Measure `usage_metadata` on ~5 real calls (`pipeline/usage_probe.py`) before quoting any LLM cost; thinking tokens are billed as output |
| L11 | 2026-10-21 | A `--samples 1` probe wrote nothing (the labeler needs ≥ 2 votes per record), so the unattended chain reported failure and scheduled a VM shutdown | Probe with the same sample count as the real run; failure paths that stop the VM must be easy to cancel (`sudo shutdown -c`) |
| L12 | 2026-10-21 | Capping labels at N=6,000 still left 3,551 to go, not 3,105: `asyncio.as_completed` processes messages in arbitrary order, so earlier partial runs labeled messages beyond the cap | Fine for cost (the extra labels are used), but if exact subsets matter, iterate in order with bounded `gather` batches |
| L13 | 2026-10-22 | The unattended chain's `sudo shutdown -h +2` never stopped the VM: Workbench wraps `shutdown` ("Shutdown intercepted. Reporting to Notebooks API...") and the VM kept running (and billing) for ~45 min | Stop Workbench VMs with `gcloud workbench instances stop` from outside the VM; for self-stop, give the VM SA permission and call the API, then verify the instance state |
| L14 | 2026-10-22 | Reporting Tier-1 escalation recall against the broad teacher `escalate` label (36% of tweets) made the router look far worse than against the TDD's actual requirement (angry OR urgent) | Evaluate against the business definition in the DoD, and report the broad label separately as a priority signal |
