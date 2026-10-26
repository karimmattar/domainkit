r"""Whether a pull request changes anything but the docs, for CI's changes job.

Reads ``git diff --no-renames --name-status`` from standard input and
prints ``code=true`` or ``code=false``, for ``$GITHUB_OUTPUT``. Docs are
``docs/**`` and the Markdown files at the top of the repository
(README.md, CHANGELOG.md, SECURITY.md). Anything else is code, Markdown
elsewhere included: the project templates' files ship in the wheel. No
paths at all is docs only: nothing for the suite to test.

Every path on a line counts, so a rename or copy is code if either side
is: a module moved into ``docs/`` leaves the package, which only the
suite can check. CI also turns rename detection off (``--no-renames``),
so a move reaches this script as a deletion and an addition. A line
without a status is read as a bare path (``git diff --name-only``). A
path git had to quote (``"docs/\303\274.md"``) does not start with
``docs/``, so it counts as code: the safe side.

Usage: ``git diff --no-renames --name-status HEAD^1 HEAD | python3
.github/scripts/changed.py``.
"""

import re
import sys

STATUS = re.compile(r"[ACDMRTUXB]\d{0,3}")
"""A ``--name-status`` status: a letter, and a similarity score for R and C."""


def paths(line: str) -> list[str]:
    """The paths a line of ``git diff`` output names: both sides of a rename."""
    fields = line.split("\t")
    if len(fields) > 1 and STATUS.fullmatch(fields[0]):
        return fields[1:]
    return [line]


def is_docs(path: str) -> bool:
    """Whether ``path`` (relative to the repository) is documentation only."""
    return path.startswith("docs/") or ("/" not in path and path.endswith(".md"))


def main() -> int:
    """Print ``code=true`` if any path read is not documentation."""
    named = [
        path for line in sys.stdin.read().splitlines() if line for path in paths(line)
    ]
    code = any(not is_docs(path) for path in named)
    print(f"code={str(code).lower()}")  # noqa: T201 - for $GITHUB_OUTPUT.
    return 0


if __name__ == "__main__":
    sys.exit(main())
