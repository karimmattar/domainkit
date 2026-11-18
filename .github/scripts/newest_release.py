"""Whether a release tag is the newest final release: the one the site shows.

docs.yml publishes the documentation site from a release tag that
release_check.py accepted, and only when this says so (ADR 0189). A
pre-release, or a patch of an older line (the security fixes the last 1.x
minor gets after 2.0), leaves the site showing the newest release.

Prints ``true`` or ``false``.

Usage: ``python3 .github/scripts/newest_release.py vX.Y.Z``, in the
repository, with its tags fetched.
"""

import subprocess
import sys

from release_check import FINAL, key


def newest(tag: str, tags: list[str]) -> bool:
    """Whether ``tag`` is a final release, and no other final release is newer.

    A pre-release of a newer line does not count: the site shows finals.
    """
    this = key(tag.removeprefix("v"))
    if this is None or this[3] != FINAL:
        return False
    others = (key(other.removeprefix("v")) for other in tags)
    return all(other is None or other[3] != FINAL or other <= this for other in others)


def main(argv: list[str]) -> int:
    """Print whether the tag ``argv[0]`` is the newest release."""
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"],  # noqa: S607 - git from PATH.
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    print("true" if newest(argv[0], tags) else "false")  # noqa: T201 - the output.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
