"""The newest final release among the tags: the one the site shows.

docs.yml publishes the documentation site of this release, whatever tag
started its run, once the release passes release.yml's tag checks
(ADR 0189). A pre-release does not count: the site shows final
releases. So a run that waited behind another, and was dropped for a
newer one, leaves nothing out: the run that follows publishes the
newest.

Prints the tag, such as ``v2.9.0``, or nothing when no final release is
tagged.

Usage: ``python3 .github/scripts/newest_release.py``, in the repository,
with its tags fetched.
"""

import subprocess
import sys

from release_check import FINAL, key


def newest(tags: list[str]) -> str | None:
    """The tag of the newest final release among ``tags``, or None."""
    finals = [
        (version, tag)
        for tag in tags
        if (version := key(tag.removeprefix("v"))) is not None and version[3] == FINAL
    ]
    return max(finals)[1] if finals else None


def main() -> int:
    """Print the newest final release's tag, or nothing."""
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"],  # noqa: S607 - git from PATH.
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    print(newest(tags) or "")  # noqa: T201 - the output.
    return 0


if __name__ == "__main__":
    sys.exit(main())
