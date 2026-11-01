"""Every package poetry.lock pins, as pip constraints.

CI's build job installs the wheel it built in a clean environment with
these (``pip install -c``), so its smoke test runs on the set the other
required jobs test, not on whatever upstream released that day: a new
release upstream is the newest job's to find, and must not fail a required
check or the run a release reuses (INFRA-21). A constraint names a version
and installs nothing by itself, so the dev and docs groups' pins do no
harm.

Usage: ``python .github/scripts/lock_constraints.py [poetry.lock] >
constraints.txt``.
"""

import sys
import tomllib
from pathlib import Path


def constraints(lock: Path) -> list[str]:
    """``name==version`` for each package ``lock`` pins, sorted."""
    packages = tomllib.loads(lock.read_text())["package"]
    return sorted(f"{package['name']}=={package['version']}" for package in packages)


def main(argv: list[str]) -> int:
    """Print the constraints for the lock file named, or this one."""
    path = Path(argv[0]) if argv else Path("poetry.lock")
    for line in constraints(path):
        print(line)  # noqa: T201 - the constraints file.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
