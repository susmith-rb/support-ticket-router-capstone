"""Print the image's pip requirements from pyproject.toml (base deps + runtime, db, agents extras).

Used by services/Dockerfile to install dependencies in a layer that depends only on
pyproject.toml, so Docker's layer cache reuses it until dependencies change (REQ-CD-003).
"""

import sys
import tomllib

EXTRAS = ("runtime", "db", "agents")


def main(path: str = "pyproject.toml") -> None:
    with open(path, "rb") as f:
        project = tomllib.load(f)["project"]
    reqs = list(project.get("dependencies", []))
    for extra in EXTRAS:
        reqs += project.get("optional-dependencies", {}).get(extra, [])
    print("\n".join(dict.fromkeys(reqs)))


if __name__ == "__main__":
    main(*sys.argv[1:])
