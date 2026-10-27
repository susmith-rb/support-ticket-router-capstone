"""Every runtime package directory must be declared in pyproject (non-editable installs/images need it)."""
# REQ-PLAT-001
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_all_ticket_router_packages_declared():
    # REQ-PLAT-001: wheels/images only include explicitly listed packages.
    declared = set(tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"])
    src = ROOT / "src"
    on_disk = {
        ".".join(p.parent.relative_to(src).parts)
        for p in src.rglob("__init__.py")
        if "__pycache__" not in p.parts
    }
    missing = sorted(on_disk - declared)
    assert not missing, f"packages missing from pyproject [tool.setuptools].packages: {missing}"
