"""Package structure and directory layout tests.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
"""

from pathlib import Path


def test_directory_structure():
    # REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
    repo_root = Path(__file__).resolve().parent.parent.parent
    agents_dir = repo_root / "src" / "ticket_router" / "agents"
    assert agents_dir.exists(), f"Agents package missing at {agents_dir}"

    triage_dir = agents_dir / "triage_router"
    assert triage_dir.exists(), f"triage_router package missing at {triage_dir}"

    expected_triage_files = [
        "__init__.py",
        "agent.py",
        "policy.py",
        "templates.py",
        "mcp_client.py",
        "a2a_client.py",
    ]
    for fname in expected_triage_files:
        fpath = triage_dir / fname
        assert fpath.exists(), f"Missing required file in triage_router: {fname}"

    # Verify importability of key exports
    from ticket_router.agents.triage_router.agent import TriageRouterAgent, root_agent
    from ticket_router.agents.triage_router.policy import RouterDecision, TriagePolicy

    assert root_agent is not None
    assert TriageRouterAgent is not None
    assert TriagePolicy is not None
    assert RouterDecision is not None
