"""Whether a pushed tag names a release made as docs/versioning.md says.

release.yml's tag job runs it on the tagged commit, after checking that
the tag is ``v`` and ``pyproject.toml``'s version. The version alone said
too little: ``main`` kept ``0.13.0`` through two milestones that were never
tagged, so ``v0.13.0`` would have published 0.15's code (INFRA-16). The tag
is refused unless:

- the version is ``X.Y.Z``, or a pre-release ``X.Y.ZaN``, ``bN``, ``rcN``;
- ``CHANGELOG.md``'s ``[Unreleased]`` section holds nothing (step 1 moved
  its entries), and its newest release section is the version's, dated
  (``## [X.Y.Z] - YYYY-MM-DD``);
- the version is newer than every other ``v*`` tag of its line (``X.Y``),
  and a new line's first release (``X.Y.0``, or its pre-releases) is newer
  than every tag: a patch of an older line, the security fixes the last
  1.x minor gets after 2.0, may follow a newer line's tags;
- the tagged commit is the release commit: the version changed in it, or
  it added the version's changelog section. A later commit of ``main``
  still carrying that version is not the release.

Prints each reason, and exits 1 if there is any.

Usage: ``python3 .github/scripts/release_check.py vX.Y.Z``, in the
repository, with its tags fetched.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?")
"""The versions a release may have: final, or a numbered pre-release."""
PRE = {"a": 0, "b": 1, "rc": 2}
"""Pre-releases by rank; a final release ranks above them all."""
FINAL = 3
SECTION = re.compile(
    r"^## \[(?P<name>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?[ \t]*$", re.M
)
"""A changelog section's heading: ``## [Unreleased]`` or ``## [X.Y.Z] - date``."""


def key(version: str) -> tuple[int, int, int, int, int] | None:
    """``version`` as a sortable key, or None if it is not a release version."""
    match = VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, pre, number = match.groups()
    rank = FINAL if pre is None else PRE[pre]
    return (int(major), int(minor), int(patch), rank, int(number or 0))


def changelog_problems(changelog: str, version: str) -> list[str]:
    """What keeps ``changelog`` from being the release of ``version``."""
    headings = list(SECTION.finditer(changelog))
    found: list[str] = []
    if headings and headings[0]["name"] == "Unreleased":
        end = headings[1].start() if len(headings) > 1 else len(changelog)
        if changelog[headings[0].end() : end].strip():
            found.append("CHANGELOG.md: [Unreleased] still holds entries")
        headings = headings[1:]
    if not headings or headings[0]["name"] != version:
        found.append(f"CHANGELOG.md: the newest release section is not [{version}]")
    elif headings[0]["date"] is None:
        found.append(f"CHANGELOG.md: [{version}] has no date")
    return found


def tag_problems(version: str, tags: list[str]) -> list[str]:
    """Why ``version`` may not follow the existing ``tags``."""
    this = key(version)
    if this is None:
        return [f"{version} is not a release version (X.Y.Z, or aN, bN, rcN)"]
    new_line = this[2] == 0
    found: list[str] = []
    for tag in tags:
        other = key(tag.removeprefix("v"))
        if other is None or tag == f"v{version}":
            continue
        if (new_line or other[:2] == this[:2]) and other >= this:
            found.append(f"{tag} exists, and {version} is not newer")
    return found


def commit_problems(
    version: str, parent_pyproject: str | None, parent_changelog: str | None
) -> list[str]:
    """Why the tagged commit is not the one that released ``version``.

    ``parent_*`` are the files in the commit before it, None for a root
    commit or a file it did not have.
    """
    if parent_pyproject is None:
        return []
    before = tomllib.loads(parent_pyproject).get("project", {}).get("version")
    sections = SECTION.finditer(parent_changelog or "")
    had_section = any(heading["name"] == version for heading in sections)
    if before == version and had_section:
        return [
            (
                f"the commit before this one already had version {version} and "
                "its changelog section: tag the release commit"
            )
        ]
    return []


def git_show(path: str) -> str | None:
    """``path`` in the tagged commit's parent, or None."""
    done = subprocess.run(  # noqa: S603 - git, fixed arguments.
        ["git", "show", f"HEAD^:{path}"],  # noqa: S607 - git from PATH.
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout if done.returncode == 0 else None


def main(argv: list[str]) -> int:
    """Print why the tag ``argv[0]`` is refused; exit 1 if it is."""
    tag = argv[0]
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    found: list[str] = []
    if tag != f"v{version}":
        found.append(f"{tag} does not match version {version} in pyproject.toml")
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"],  # noqa: S607 - git from PATH.
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    found += tag_problems(version, tags)
    found += changelog_problems(Path("CHANGELOG.md").read_text(), version)
    found += commit_problems(
        version, git_show("pyproject.toml"), git_show("CHANGELOG.md")
    )
    for line in found:
        print(line)  # noqa: T201 - the release's log.
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
