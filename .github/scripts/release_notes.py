"""The CHANGELOG's section for one version, as a GitHub Release's notes.

release.yml's github-release job writes the notes of the release it makes
with this: the entries under ``## [X.Y.Z] - date``, up to the next
version's heading, without the heading itself (the release is titled with
its tag). A version the CHANGELOG has no section for, or an empty one, is
an error: a release without notes is a release nobody can read.

Usage: ``python .github/scripts/release_notes.py X.Y.Z [CHANGELOG.md] > notes.md``.
"""

import re
import sys
from pathlib import Path


def notes(changelog: str, version: str) -> str:
    """The entries of ``version``'s section of ``changelog``."""
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ValueError(f"the CHANGELOG has no section for {version}")
    section = changelog[match.end() :].split("\n## [", 1)[0].strip()
    if not section:
        raise ValueError(f"the CHANGELOG's section for {version} is empty")
    return section + "\n"


def main(argv: list[str]) -> int:
    """Print the notes of the version named, from the CHANGELOG named or this one."""
    version, *rest = argv
    path = Path(rest[0]) if rest else Path(__file__).parents[2] / "CHANGELOG.md"
    try:
        sys.stdout.write(notes(path.read_text(encoding="utf-8"), version))
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
