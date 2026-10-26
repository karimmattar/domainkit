"""The floor of every range in pyproject.toml, as pip constraints.

CI's oldest job installs the wheel with these (``pip install -c``): each
dependency and extra at the oldest version its range allows, and what they
need at the newest their own ranges allow, since pip resolves no other way.
A requirement without a ``>=`` floor is an error: ADR 0075 says every range
starts at a version CI tests.

Usage: ``python .github/scripts/floors.py [pyproject.toml] > floors.txt``.
"""

import re
import sys
import tomllib
from pathlib import Path

# A name, its extras, then the first ">=" bound, with or without
# parentheses (PEP 508 as Poetry writes it).
FLOOR = re.compile(r"([A-Za-z0-9._-]+)(?:\[[^\]]*\])?\s*\(?\s*>=\s*([^,)\s;]+)")


def floors(pyproject: Path) -> dict[str, str]:
    """Each runtime requirement's name, lowercased, and its floor."""
    project = tomllib.loads(pyproject.read_text())["project"]
    requirements = list(project["dependencies"])
    for extra in project["optional-dependencies"].values():
        requirements += extra
    found: dict[str, str] = {}
    for requirement in requirements:
        match = FLOOR.match(requirement)
        if match is None:
            raise ValueError(f"no >= floor in {requirement!r}")
        name, floor = match.group(1).lower(), match.group(2)
        if found.setdefault(name, floor) != floor:
            raise ValueError(f"{name} has two floors: {found[name]} and {floor}")
    return found


def main(argv: list[str]) -> int:
    """Print the constraints for the pyproject.toml named, or this one."""
    path = Path(argv[0]) if argv else Path("pyproject.toml")
    for name, floor in sorted(floors(path).items()):
        print(f"{name}=={floor}")  # noqa: T201 - the constraints file.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
